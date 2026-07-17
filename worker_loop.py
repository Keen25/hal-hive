#!/usr/bin/env python3
"""
HAL — Worker loop (Phase 9.1 §5)

A self-driving worker: long-poll the queue for a task, run it, push the branch,
report the outcome, repeat. Survives HAL restarts (the queue is the state).

Two run modes:
  local  — run copilot as a local subprocess on this host (the HAL lane).
           Talks to the queue via the REST server (default) or in-process.
  remote — this same script runs ON a Windows worker (launched once at boot via
           a scheduled task), reaching the queue over HTTP to HAL.

While a task runs, a background heartbeat thread renews the lease every 60s. If
the heartbeat comes back "not alive" (the reaper requeued us), we abandon the run.

Usage:
    python3 worker_loop.py --worker-id hal#1 --lane hal
    python3 worker_loop.py --worker-id ar1#1 --lane ar1 --queue http://192.168.1.14:5001
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

import requests

import hive_queue as q   # only used for LAB_GIT path + local git; DB via REST
import hive_log
try:
    from hive_version import CODE_VERSION
except Exception:        # pragma: no cover - hive_version is always co-deployed
    CODE_VERSION = 0

log = hive_log.get_logger('hive.worker')

# Static self-reported identity — sent on every /claim so the registry always
# knows WHERE each worker actually runs (the install dir), on which host, and
# with which python. Prevents "pushed to the wrong copy" blips (e.g. a worker
# running from D:\hive while an operator assumes C:\hive). Computed once.
INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
HOSTNAME    = socket.gethostname()
PYTHON_EXE  = sys.executable or ''

# How much of the worker copilot log to ship back to HAL on /complete (U2 #16).
# The full JSONL can be large; the tail is what matters for failure triage.
LOG_TAIL_BYTES = int(os.environ.get('HIVE_LOG_TAIL_BYTES', '16384'))


def _read_log_tail(path: str | None, max_bytes: int = LOG_TAIL_BYTES) -> str | None:
    """Return the last ``max_bytes`` of the worker copilot log as text, or None.
    Reads from the end so a multi-MB JSONL never loads fully into memory."""
    if not path or not os.path.exists(path):
        return None
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            data = f.read()
        text = data.decode('utf-8', errors='replace')
        if size > max_bytes:
            text = f'…[truncated, showing last {max_bytes} of {size} bytes]…\n' + text
        return text
    except OSError as e:
        return f'[worker: could not read log tail: {e}]'


def _parse_usage_from_log(path: str | None) -> dict | None:
    """Scan the copilot JSONL log for the LAST line whose parsed JSON has
    type == "result" and return its ``usage`` dict, or None on any failure.

    The ``{"type":"result"}`` line is NOT always the final physical line —
    trailing ``session.background_tasks_changed`` ephemeral events may follow
    it — so we scan every line and keep the last match."""
    if not path or not os.path.exists(path):
        return None
    last_usage = None
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get('type') == 'result' and 'usage' in obj:
                        last_usage = obj['usage']
                except Exception:
                    pass
    except OSError:
        pass
    return last_usage


# How much of the LIVE copilot log to scan each heartbeat for a progress
# snapshot. Only the tail matters ("what is it doing NOW"); a large multi-MB
# JSONL is never fully read on the hot heartbeat path.
PROGRESS_TAIL_BYTES = int(os.environ.get('HIVE_PROGRESS_TAIL_BYTES', '65536'))


class _ProgressHolder:
    """Shared, mutable handle the Heartbeat reads to build a live snapshot.
    The copilot log path is only born INSIDE run_task/run_review_task (mkstemp),
    after the Heartbeat is already constructed in loop(); this holder bridges the
    two so the heartbeat thread can tail the in-flight log. `log_path` is None
    until the run sets it (heartbeat is a no-op progress-wise until then)."""
    __slots__ = ('log_path', 'started_at')

    def __init__(self):
        self.log_path = None
        self.started_at = time.time()


def _progress_snapshot(log_path: str | None, started_at: float | None) -> dict | None:
    """Build a compact in-flight progress snapshot from the tail of the live
    copilot JSONL log. Returns None until the log exists / has parseable events.

    Fields: last_tool (toolName + a human-legible arg + turn), last_msg (latest
    assistant text), turn (max turnId seen in the tail), elapsed_s, phase
    ('working' | 'finishing' once the terminal result frame appears). Cheap: tail
    read only, unparseable/partial first line silently skipped."""
    if not log_path or not os.path.exists(log_path):
        return None
    try:
        size = os.path.getsize(log_path)
        with open(log_path, 'rb') as f:
            if size > PROGRESS_TAIL_BYTES:
                f.seek(-PROGRESS_TAIL_BYTES, os.SEEK_END)
            data = f.read()
    except OSError:
        return None
    last_tool = None
    last_msg = None
    turn = 0
    result_seen = False
    for line in data.decode('utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue  # partial first line from the tail cut, or non-JSON noise
        t = o.get('type')
        if t == 'tool.execution_start':
            d = o.get('data') or {}
            args = d.get('arguments') or {}
            detail = (args.get('description') or args.get('command')
                      or args.get('path') or args.get('summary') or '')
            last_tool = {'tool': d.get('toolName'),
                         'detail': str(detail)[:120],
                         'turn': d.get('turnId')}
            try:
                turn = max(turn, int(d.get('turnId')))
            except (TypeError, ValueError):
                pass
        elif t == 'assistant.message':
            txt = o.get('text') or o.get('content')
            if isinstance(txt, str) and txt.strip():
                last_msg = txt.strip()[:160]
        elif t == 'result':
            result_seen = True
    if last_tool is None and last_msg is None and turn == 0 and not result_seen:
        return None  # nothing meaningful yet
    snap = {
        'turn': turn,
        'elapsed_s': int(time.time() - started_at) if started_at else None,
        'phase': 'finishing' if result_seen else 'working',
    }
    if last_tool:
        snap['last_tool'] = last_tool
    if last_msg:
        snap['last_msg'] = last_msg
    return snap


HEARTBEAT_INTERVAL_S = 60
POLL_BACKOFF_S       = 3         # sleep when /claim returns empty (204)
COPILOT_TIMEOUT_S    = int(os.environ.get('COPILOT_TIMEOUT_S', '840'))

# S3 (#6): keep every per-task artifact (clone, log, prompt) under one directory
# the operator can lock down with an ACL, instead of the account-wide system
# TEMP. On the least-privilege Windows worker the bootstrap points this at
# C:\hive\work (Full Control: svc-hive only). Unset = use the system default
# temp dir (prior behaviour), so nothing changes on hosts that don't set it.
WORK_DIR = os.environ.get('HIVE_WORK_DIR') or None
if WORK_DIR:
    os.makedirs(WORK_DIR, exist_ok=True)

# S3 (#6): the copilot permission flags are overridable so an operator can scope
# the agent down (e.g. per-tool allows) WITHOUT editing code. The default is the
# lab-proven set — a narrower `--allow-all-tools` blocks writes outside CWD and
# breaks real tasks — so existing workers are unaffected. Set HIVE_COPILOT_PERMS
# (a shell-quoted flag string, may be empty) to override.
_DEFAULT_COPILOT_PERMS = ['--no-ask-user', '--allow-all', '--autopilot']


def _copilot_perms() -> list[str]:
    override = os.environ.get('HIVE_COPILOT_PERMS')
    if override is None:
        return list(_DEFAULT_COPILOT_PERMS)
    return shlex.split(override)


def _worker_verify(cwd: str) -> tuple[bool, bool, str]:
    """Run the build/test gate in the worker's OWN clone (Sc1 #10) so verify
    happens in parallel across workers instead of serially in the merge loop.

    Returns (ran, ok, summary). ``ran`` is False when HIVE_VERIFY_CMD is unset —
    the worker then reports no verify and the merge loop runs the gate as before
    (fully backward compatible). Uses the same HIVE_VERIFY_CMD contract as the
    server-side gate, but read from the worker's own environment."""
    cmd = os.environ.get('HIVE_VERIFY_CMD')
    if not cmd:
        return False, False, 'no HIVE_VERIFY_CMD on worker'
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              cwd=cwd, timeout=COPILOT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return True, False, 'worker verify timed out'
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-5:]
    return True, proc.returncode == 0, ' | '.join(tail)


def _copilot_argv(prompt_ref: str, model: str | None = None) -> list[str]:
    """Build the copilot invocation. The executable is configurable via
    HIVE_COPILOT_CMD because the CLI is not always named `copilot` on PATH —
    on Windows npm installs a `copilot.cmd` shim that CreateProcess cannot
    launch via a bare `copilot` (PATHEXT is not applied to argv[0]). Point
    HIVE_COPILOT_CMD at the full shim path there, e.g.
    C:\\npm-global\\copilot.cmd.

    ``prompt_ref`` is a copilot ``-p`` argument. Callers pass ``@<file>`` so the
    prompt is read from a file rather than argv: a large prompt (e.g. a PR diff
    embedded for review) overflows the Windows CreateProcess command-line limit
    (~32 KB) and fails with WinError 206. A file reference is a few bytes and
    works identically on every platform.

    ``model`` (from the task's complexity tier) is passed through as ``--model``
    so cheap work runs on a cheap model and complex work on a frontier one. When
    absent, copilot uses its configured default."""
    exe = os.environ.get('HIVE_COPILOT_CMD', 'copilot')
    argv = [exe, '-p', prompt_ref, *_copilot_perms(), '--output-format', 'json']
    if model:
        argv += ['--model', model]
    return argv


# ── Queue client (REST) ───────────────────────────────────────────────────────

class QueueClient:
    # Latest self-update signal from the server, refreshed on every /claim
    # (present on the 204 long-poll return too). None until the first claim.
    server_code_version: int | None = None
    update_to: int | None = None

    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip('/')
        self.h = {'Authorization': f'Bearer {token}'}

    def claim(self, worker_id: str, lane: str | None, caps: list[str] | None = None):
        r = requests.post(f'{self.base}/claim',
                          json={'worker_id': worker_id, 'lane': lane, 'caps': caps,
                                'code_version': CODE_VERSION,
                                'install_dir': INSTALL_DIR, 'hostname': HOSTNAME,
                                'python_exe': PYTHON_EXE},
                          headers=self.h, timeout=40)
        # Refresh the self-update signal from response headers (present on both
        # 200 and the empty 204 long-poll return).
        self.server_code_version = self._int_header(r, 'X-Hive-Code-Version')
        self.update_to = self._int_header(r, 'X-Hive-Update-To')
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _int_header(resp, name: str) -> int | None:
        raw = resp.headers.get(name)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def get_version(self) -> dict:
        r = requests.get(f'{self.base}/version', headers=self.h, timeout=30)
        r.raise_for_status()
        return r.json()

    def download(self, name: str) -> bytes:
        r = requests.get(f'{self.base}/download/{name}', headers=self.h, timeout=60)
        r.raise_for_status()
        return r.content

    def heartbeat(self, task_id: str, worker_id: str,
                  lease_token: str | None = None,
                  progress: str | None = None) -> bool:
        body = {'task_id': task_id, 'worker_id': worker_id,
                'lease_token': lease_token}
        if progress is not None:
            body['progress'] = progress
        r = requests.post(f'{self.base}/heartbeat',
                          json=body, headers=self.h, timeout=15)
        r.raise_for_status()
        return r.json().get('alive', False)

    def complete(self, **kw) -> str:
        r = requests.post(f'{self.base}/complete', json=kw, headers=self.h, timeout=30)
        r.raise_for_status()
        return r.json().get('status')

    def post_review(self, author_id: str, verdict: str, findings: str,
                    review_task_id: str, usage_json: str | None = None) -> str:
        """POST a reviewer verdict to the author task. The server closes the
        review task (to 'reviewed') itself, so the worker never calls /complete
        for a review task. `usage_json` (best-effort) is stored on the review
        task so reviewing workers appear in the usage rollup."""
        body = {'verdict': verdict, 'findings': findings,
                'review_task_id': review_task_id}
        if usage_json is not None:
            body['usage_json'] = usage_json
        r = requests.post(f'{self.base}/task/{author_id}/review',
                          json=body, headers=self.h, timeout=30)
        r.raise_for_status()
        return r.json().get('result')


# ── Heartbeat thread ──────────────────────────────────────────────────────────

class Heartbeat:
    """Renews the lease every HEARTBEAT_INTERVAL_S; signals abandon if reaped.

    When given a `progress` holder it also ships a compact live snapshot of the
    in-flight copilot run (last tool call, turn, elapsed) on every beat, so the
    dashboard shows what a busy worker is doing NOW instead of a blank box until
    completion. Progress is best-effort: any failure to build/attach it never
    affects lease renewal (the abandon signal)."""
    def __init__(self, client: QueueClient, task_id: str, worker_id: str,
                 lease_token: str | None = None,
                 progress: '_ProgressHolder | None' = None):
        self.client, self.task_id, self.worker_id = client, task_id, worker_id
        self.lease_token = lease_token
        self.progress = progress
        self.abandon = threading.Event()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            snap_json = None
            if self.progress is not None:
                try:
                    snap = _progress_snapshot(self.progress.log_path,
                                              self.progress.started_at)
                    if snap is not None:
                        snap_json = json.dumps(snap)
                except Exception as e:
                    log.debug('progress snapshot failed for %s: %s', self.task_id, e)
            try:
                if not self.client.heartbeat(self.task_id, self.worker_id,
                                             self.lease_token, progress=snap_json):
                    log.warning('heartbeat: lease lost for %s — abandoning', self.task_id)
                    self.abandon.set()
                    return
            except Exception as e:
                log.warning('heartbeat error: %s', e)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()


# ── The run ───────────────────────────────────────────────────────────────────

def _git_auth_args() -> list:
    """Inject the shared swarm token as an HTTP header for smart-HTTP git ops.

    The lab.git transport (git_http_server.py) now requires token auth. We pass
    the token per-invocation via `-c http.extraheader=...` rather than embedding
    it in the remote URL, so it never lands in .git/config on the throwaway clone.
    HIVE_TOKEN is already exported to every worker service (WinSW/systemd/launchd).
    """
    tok = (os.environ.get('HIVE_GIT_TOKEN') or os.environ.get('HIVE_TOKEN') or '').strip()
    if tok:
        return ['-c', f'http.extraheader=Authorization: Bearer {tok}']
    return []


def _repo_url(lab_git: str, repo: str | None) -> str:
    """Map a task's `repo` name onto a clone URL/path, sibling to the lab.git base.

    The worker is configured with one `lab_git` endpoint (a smart-HTTP URL like
    `http://192.168.100.1:8418/lab.git` on remote workers, or a local bare path
    `~/hive/lab.git` on the hal lane). Repo routing swaps just the final path
    segment, so `ActiveRoles.git` resolves to the mirror served alongside lab.git
    by the same token-authed git_http_server. Default/omitted -> the lab_git
    endpoint unchanged (backward compatible for every existing task)."""
    repo = (repo or 'lab.git').strip()
    if repo in ('', 'lab.git'):
        return lab_git
    base, sep, _name = lab_git.rstrip('/').rpartition('/')
    return f'{base}{sep}{repo}' if sep else repo


def _clone_source(task: dict, lab_git: str) -> tuple[str, bool]:
    """Return (clone_url, is_mirror_repo) for a task. Mirror repos (non-lab.git)
    are full source trees seeded shallow from an upstream (ADO); they are cloned
    WITHOUT the blobless filter because the worker must materialize every blob to
    build, and a shallow+blobless combination is fragile."""
    repo = task.get('repo') or 'lab.git'
    return _repo_url(lab_git, repo), (repo not in ('', 'lab.git'))


def _provision_build_secrets(task: dict, base_url: str, token: str) -> None:
    """Just-in-time NuGet credential provisioning (replaces the external Windows
    scheduled-task hack `hive-nuget-cred` + `refresh-nuget-cred.ps1` on ar1).

    When a task will restore private .NET packages, fetch a FRESH Azure DevOps
    token from the queue server's control-token-gated `/secret/nuget` endpoint and
    write a NuGet.Config (packageSourceCredentials only) into the NuGet user-config
    path(s) that headless MSBuild — running as THIS worker's OS identity — reads
    ambiently. A fresh token every task means no timer and no expiry drift.

    Best-effort and NON-FATAL: any failure (not a build task, network, non-200,
    bad JSON, unwritable path) is logged and swallowed — the task may not even
    need packages, or the restore cache may already be warm. The token is written
    ONLY into the config file(s); it is never logged.
    """
    # Signal that the run will restore .NET packages. If absent, do nothing and
    # make no network call — non-dotnet / non-build tasks are wholly unaffected.
    requires = task.get('requires') or []
    if 'build:dotnet' not in requires:
        return

    url = f'{base_url.rstrip("/")}/secret/nuget'
    try:
        r = requests.get(url, headers={'Authorization': 'Bearer ' + token},
                         timeout=15)
        r.raise_for_status()
        secret = r.json()
    except Exception as e:
        log.warning('nuget secret fetch failed (%s); continuing without creds', e)
        return

    username = secret.get('username')
    password = secret.get('password')
    feeds = secret.get('feeds') or []
    if not username or not password or not feeds:
        log.warning('nuget secret payload incomplete; continuing without creds')
        return

    # packageSourceCredentials only — this merges with the repo's own
    # <packageSources> at restore time (source key must match the feed name the
    # repo's NuGet.Config uses). ClearTextPassword because the token is short-TTL
    # and machine-encrypted password entries are not portable across identities.
    from xml.sax.saxutils import escape

    def _cred_block(feed: dict) -> str:
        # NuGet source keys with spaces/@ are stored XML-encoded (e.g. a space ->
        # "_x0020_"); the feed names here ("OneIdentity", "Starling2FA") are plain
        # identifiers, so use them verbatim. escape() guards value text regardless.
        key = feed.get('name', '')
        return (f'    <{key}>\n'
                f'      <add key="Username" value="{escape(username)}" />\n'
                f'      <add key="ClearTextPassword" value="{escape(password)}" />\n'
                f'    </{key}>')

    creds = '\n'.join(_cred_block(f) for f in feeds if f.get('name'))
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<configuration>\n'
           '  <packageSourceCredentials>\n'
           f'{creds}\n'
           '  </packageSourceCredentials>\n'
           '</configuration>\n')

    targets = _nuget_config_targets()
    written = []
    for path in targets:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(xml)
            written.append(path)
        except OSError as e:
            log.warning('could not write NuGet.Config to %s: %s', path, e)

    if written:
        # Log paths + expiry for auditability; NEVER the token itself.
        log.info('provisioned NuGet creds (%d feed(s), expires %s) -> %s',
                 len(feeds), secret.get('expiresOn', '?'), '; '.join(written))


def _nuget_config_targets() -> list[str]:
    """NuGet user-config path(s) for the current OS + identity so headless
    MSBuild picks the creds up ambiently.

    Windows: the ar1 worker runs as LocalSystem and the ARS build uses the 32-bit
    MSBuild whose %APPDATA% is WOW64-redirected. When running under the SYSTEM
    profile we can't be sure which view (System32 vs SysWOW64) MSBuild resolves,
    so write BOTH systemprofile locations (harmless over-write beats a missed
    path). When not SYSTEM, write the normal %APPDATA%\\NuGet\\NuGet.Config.

    POSIX: ~/.nuget/NuGet/NuGet.Config (honouring $NUGET_CONFIG if it points at a
    dir). Keeps the helper cross-platform and a no-op-safe on hal."""
    if os.name == 'nt':
        appdata = os.environ.get('APPDATA', '')
        # "Am I SYSTEM?": the LocalSystem profile's APPDATA lives under
        # ...\systemprofile\... — a robust, identity-based check that doesn't rely
        # on USERNAME spoofing. If unset (service with no profile) treat as SYSTEM.
        is_system = ('systemprofile' in appdata.lower()) or not appdata
        if is_system:
            windir = os.environ.get('WINDIR', r'C:\Windows')
            paths = [
                os.path.join(windir, 'System32', 'config', 'systemprofile',
                             'AppData', 'Roaming', 'NuGet', 'NuGet.Config'),
                os.path.join(windir, 'SysWOW64', 'config', 'systemprofile',
                             'AppData', 'Roaming', 'NuGet', 'NuGet.Config'),
            ]
            # Also write the resolved APPDATA path when present, in case the
            # service DID load a systemprofile env that differs from the above.
            if appdata:
                paths.append(os.path.join(appdata, 'NuGet', 'NuGet.Config'))
            return paths
        return [os.path.join(appdata, 'NuGet', 'NuGet.Config')]

    # POSIX. $NUGET_CONFIG, when set, names a directory NuGet searches.
    cfg_dir = os.environ.get('NUGET_CONFIG')
    if cfg_dir and os.path.isdir(cfg_dir):
        return [os.path.join(cfg_dir, 'NuGet.Config')]
    home = os.path.expanduser('~')
    return [os.path.join(home, '.nuget', 'NuGet', 'NuGet.Config')]


def run_review_task(task: dict, worker_id: str, lab_git: str,
                    progress: '_ProgressHolder | None' = None) -> dict:
    """Run a peer-review task: clone lab.git, checkout the AUTHOR branch, run
    copilot with the review prompt (read-only intent — the prompt forbids
    push/edit), then parse HIVE_REVIEW_VERDICT.json from the workspace.

    Returns {'verdict', 'findings', 'exit_code', 'log_path'}. If the verdict file
    is missing/unparseable, fails safe to REQUEST_CHANGES (never auto-approves).
    Does NOT commit or push — reviewing only reads the diff.

    `progress` (optional): a holder the heartbeat tails for a live snapshot; we
    publish the copilot log path onto it as soon as the log exists.
    """
    # A review task's OWN branch (feature/<review_id>) never exists in lab.git —
    # the reviewer must check out the AUTHOR's branch under review, delivered as
    # `review_branch` in the claim payload. Fall back to task['branch'] only if a
    # server predating that field is in play.
    branch = task.get('review_branch') or task['branch']
    base_ref, tid = task['base_ref'], task['id']
    run_dir = tempfile.mkdtemp(prefix=f'{tid}-', dir=WORK_DIR)
    log_fd, log_path = tempfile.mkstemp(prefix=f'{tid}-', suffix='.jsonl', dir=WORK_DIR)
    os.close(log_fd)
    if progress is not None:
        progress.log_path = log_path
    prompt_fd, prompt_path = tempfile.mkstemp(prefix=f'{tid}-', suffix='.prompt', dir=WORK_DIR)
    with os.fdopen(prompt_fd, 'w', encoding='utf-8') as pf:
        pf.write(task['prompt'])

    auth = _git_auth_args()

    def g(*a, cwd=run_dir):
        return subprocess.run(['git', *auth, *a], cwd=cwd, capture_output=True, text=True)

    fail_safe = {'verdict': 'REQUEST_CHANGES',
                 'findings': 'reviewer produced no parseable verdict; failing safe (no auto-approve)',
                 'log_path': log_path}
    try:
        clone_url, is_mirror = _clone_source(task, lab_git)
        clone_filter = '' if is_mirror else os.environ.get('HIVE_CLONE_FILTER', 'blob:none').strip()
        clone_args = ['clone']
        if clone_filter:
            clone_args.append(f'--filter={clone_filter}')
        clone_args += [clone_url, run_dir]
        cl = subprocess.run(['git', *auth, *clone_args], capture_output=True, text=True)
        if cl.returncode != 0:
            fail_safe['findings'] = f'review clone failed: {cl.stderr.strip()}'
            fail_safe['exit_code'] = 1
            return fail_safe

        # Check out the author branch so copilot reviews the actual change.
        co = g('checkout', branch)
        if co.returncode != 0:
            # Fall back to fetching the branch explicitly (blobless clone may not
            # have it as a local ref yet).
            g('fetch', 'origin', branch)
            co = g('checkout', branch)
        if co.returncode != 0:
            fail_safe['findings'] = f'could not checkout review branch {branch}: {co.stderr.strip()}'
            fail_safe['exit_code'] = 1
            return fail_safe

        with open(log_path, 'w') as logf:
            proc = subprocess.run(
                _copilot_argv(f'@{prompt_path}', model=task.get('model')),
                cwd=run_dir, stdout=logf, stderr=subprocess.STDOUT,
                timeout=COPILOT_TIMEOUT_S, text=True,
            )

        verdict, findings = _read_review_verdict(run_dir)
        out = {'verdict': verdict, 'findings': findings,
               'exit_code': proc.returncode, 'log_path': log_path}
        # Capture copilot billing usage for the review pass too, so reviewing
        # workers show up in the usage rollup (not just authors).
        try:
            usage = _parse_usage_from_log(log_path)
            if usage is not None:
                out['usage_json'] = json.dumps(usage)
        except Exception:
            pass
        return out
    except subprocess.TimeoutExpired:
        fail_safe['findings'] = 'reviewer timed out; failing safe (no auto-approve)'
        fail_safe['exit_code'] = 124
        return fail_safe
    except Exception as e:
        fail_safe['findings'] = f'reviewer error: {e}; failing safe (no auto-approve)'
        fail_safe['exit_code'] = 1
        return fail_safe
    finally:
        try:
            os.remove(prompt_path)
        except OSError:
            pass
        shutil.rmtree(run_dir, ignore_errors=True)


def _read_review_verdict(run_dir: str) -> tuple[str, str]:
    """Read HIVE_REVIEW_VERDICT.json from the reviewer workspace. Returns
    (verdict, findings). Fails safe to REQUEST_CHANGES on any problem so a
    missing/garbled verdict can never auto-approve a branch."""
    path = os.path.join(run_dir, 'HIVE_REVIEW_VERDICT.json')
    safe = ('REQUEST_CHANGES',
            'reviewer produced no parseable verdict; failing safe (no auto-approve)')
    if not os.path.exists(path):
        return safe
    try:
        with open(path, encoding='utf-8') as f:
            obj = json.load(f)
        verdict = obj.get('verdict')
        findings = obj.get('findings', '') or ''
        if verdict not in ('APPROVE', 'REQUEST_CHANGES'):
            return safe
        return verdict, findings
    except Exception:
        return safe


def _resume_digest(g, base_sha: str, attempt) -> str | None:
    """Build a resume preamble from prior committed work on the task branch.

    A timed-out attempt WIP-commits its partial work to the task branch (see the
    TimeoutExpired handler). When a later attempt re-claims the task it clones
    that branch back, so `base_sha..HEAD` is exactly the prior progress. Summarise
    it (commit log + diffstat) so the model CONTINUES instead of restarting from
    zero. Returns None when there is nothing on top of base (fresh start)."""
    if not base_sha:
        return None
    log_oneline = g('log', '--oneline', f'{base_sha}..HEAD').stdout.strip()
    if not log_oneline:
        return None
    diffstat = g('diff', '--stat', f'{base_sha}..HEAD').stdout.strip()
    return (
        f"=== RESUME (attempt {attempt}) — prior partial work is ALREADY COMMITTED on this branch ===\n"
        "A previous attempt timed out before finishing. Its work is committed on this\n"
        "branch; do NOT redo it from scratch. Inspect it first, then finish the rest:\n"
        f"  git log --oneline {base_sha[:12]}..HEAD\n"
        f"  git diff {base_sha[:12]}..HEAD\n\n"
        "Prior commits:\n" + log_oneline + "\n\n"
        "Files touched so far:\n" + (diffstat or '(none)') + "\n\n"
        "Verify each item below against what is already done; if prior work is broken\n"
        "or incomplete, fix it. Everything must COMPILE and tests must PASS.\n"
        "=== ORIGINAL TASK ===\n"
    )


def _wip_commit_push(g, branch: str, tid: str, attempt) -> str | None:
    """Preserve a timed-out attempt's partial work: commit whatever is in the
    working tree and push it to the task branch, so the NEXT attempt resumes from
    it instead of a clean base clone. Best-effort — never raises; returns a short
    status string for the error field (or None if nothing was preserved)."""
    try:
        g('add', '-A')
        if g('diff', '--cached', '--quiet').returncode == 0:
            return None  # nothing changed to preserve
        ci = g('commit', '-m', f'{tid}: WIP [hive-resume] attempt {attempt}')
        if ci.returncode != 0:
            return None
        # ff on resume (WIP already on remote), plain create on first attempt;
        # --force-with-lease is safe (hive owns this branch) and covers the rare
        # case where a finalize collapse rewrote history under us.
        push = g('push', '--force-with-lease', 'origin', branch)
        if push.returncode != 0:
            return f'WIP preserve push failed: {push.stderr.strip()[:120]}'
        return 'WIP preserved on branch'
    except Exception as e:
        return f'WIP preserve error: {e}'


def run_task(task: dict, worker_id: str, lab_git: str,
             progress: '_ProgressHolder | None' = None) -> dict:
    """
    Clone lab.git, branch, run copilot headless, commit, push. Returns dict with
    exit_code, commit_hash, log_path, error.

    `progress` (optional): a holder the heartbeat tails for a live snapshot; we
    publish the copilot log path onto it as soon as the log exists.

    Resume: if a prior attempt timed out, its partial work is WIP-committed to the
    task branch. A re-claim clones that branch back, prepends a resume digest to
    the prompt so the model continues, and folds the WIP into ONE coherent commit
    on success.
    """
    branch, base_ref, tid = task['branch'], task['base_ref'], task['id']
    run_dir = tempfile.mkdtemp(prefix=f'{tid}-', dir=WORK_DIR)
    # Log lives OUTSIDE the clone so `git add -A` never commits it (a log inside
    # run_dir pollutes every commit and causes cross-task rebase conflicts).
    log_fd, log_path = tempfile.mkstemp(prefix=f'{tid}-', suffix='.jsonl', dir=WORK_DIR)
    os.close(log_fd)
    if progress is not None:
        progress.log_path = log_path
    # Prompt also lives outside the clone and is fed to copilot via `-p @file`
    # (see _copilot_argv): a large embedded prompt overflows the Windows argv
    # limit (WinError 206). Writing it inside run_dir would also get committed.
    prompt_fd, prompt_path = tempfile.mkstemp(prefix=f'{tid}-', suffix='.prompt', dir=WORK_DIR)
    os.close(prompt_fd)  # content written after resume detection (may prepend a digest)

    auth = _git_auth_args()

    def g(*a, cwd=run_dir):
        # Auth header is harmless on local ops and required on the network ones
        # (push); prepend it to every git call so remote-hitting subcommands work.
        return subprocess.run(['git', *auth, *a], cwd=cwd, capture_output=True, text=True)

    try:
        # Sc4 (#13): blobless partial clone — fetch commits+trees now, blobs on
        # demand. `git checkout base_ref` below materializes the working tree
        # (fetching just those blobs over the same authed transport), so copilot
        # sees a full tree while clone cost stops scaling with repo size × tasks.
        # Needs uploadpack.allowFilter on the server (git_http_server sets it);
        # against a server without it git prints a warning and falls back to a
        # full clone, so this is safe either way. HIVE_CLONE_FILTER='' disables.
        # Repo routing: mirror repos (ActiveRoles.git) clone FULL (no filter) —
        # they are shallow source trees and the worker must build them.
        clone_url, is_mirror = _clone_source(task, lab_git)
        clone_filter = '' if is_mirror else os.environ.get('HIVE_CLONE_FILTER', 'blob:none').strip()
        clone_args = ['clone']
        if clone_filter:
            clone_args.append(f'--filter={clone_filter}')
        clone_args += [clone_url, run_dir]
        cl = subprocess.run(['git', *auth, *clone_args],
                            capture_output=True, text=True)
        if cl.returncode != 0:
            return {'exit_code': 1, 'error': f'clone failed: {cl.stderr.strip()}',
                    'log_path': log_path}

        # base_ref may not exist yet on a fresh repo — branch from HEAD if so.
        if g('rev-parse', '--verify', f'origin/{base_ref}').returncode == 0:
            g('checkout', base_ref)
        # Sc1 (#10): record the base sha the branch is built on, so the merge
        # loop can tell whether the base moved (rebase drift) before trusting a
        # worker-side verify.
        base_sha = g('rev-parse', 'HEAD').stdout.strip() or None
        # Resume: if a prior (timed-out) attempt already pushed partial work to the
        # task branch, continue from it rather than a clean base — otherwise every
        # timeout throws away all progress and long jobs can never converge. A
        # git-native signal (does origin/<branch> exist?) needs no schema change
        # and is cross-worker safe: any worker that re-claims sees the WIP.
        resume = g('rev-parse', '--verify', f'origin/{branch}').returncode == 0
        if resume:
            g('checkout', '-B', branch, f'origin/{branch}')
        else:
            g('checkout', '-b', branch)
        g('config', 'user.email', 'hal@lab')
        g('config', 'user.name', f'HAL worker {worker_id}')

        # Write the prompt now (after resume detection): on a resume, prepend a
        # digest of the prior committed work so the model continues instead of
        # restarting. On a fresh attempt this is just the task prompt.
        prompt_body = task['prompt']
        if resume:
            digest = _resume_digest(g, base_sha, task.get('attempts'))
            if digest:
                prompt_body = digest + '\n' + prompt_body
                log.info('worker %s resuming %s from prior WIP on %s',
                         worker_id, tid, branch)
        with open(prompt_path, 'w', encoding='utf-8') as pf:
            pf.write(prompt_body)

        # Just-in-time build-credential provisioning (replaces the ar1 scheduled
        # task). Do it after clone/branch prep and BEFORE copilot/build starts so
        # a headless MSBuild restore finds fresh feed creds. Best-effort: wrap in
        # try/except so a secret-fetch failure can NEVER break a task. Sources the
        # queue base_url + token from the same env the worker already uses.
        try:
            _secret_base = os.environ.get('HIVE_QUEUE_URL', 'http://127.0.0.1:5001')
            _secret_tok = (os.environ.get('HIVE_TOKEN')
                           or os.environ.get('HIVE_GIT_TOKEN') or '').strip()
            _provision_build_secrets(task, _secret_base, _secret_tok)
        except Exception as e:
            log.warning('build-secret provisioning error (ignored): %s', e)

        # NB: bind the file handle to `logf`, NOT `log` — `log` is the module
        # logger (defined at import) and a `with ... as log` would shadow it for
        # the rest of this function, so the `log.info(...)` after the verify step
        # would crash on the closed file object (AttributeError), turning an
        # already-pushed task into a reported failure.
        with open(log_path, 'w') as logf:
            proc = subprocess.run(
                _copilot_argv(f'@{prompt_path}', model=task.get('model')),
                cwd=run_dir, stdout=logf, stderr=subprocess.STDOUT,
                timeout=COPILOT_TIMEOUT_S, text=True,
            )

        g('add', '-A')
        # On a resume, fold prior WIP commits + this attempt's work into ONE
        # coherent commit on base (the deliverable contract is a single commit).
        # reset --soft moves the branch ref back to base while keeping every change
        # staged, so the commit below captures the full cumulative diff.
        if resume and base_sha:
            g('reset', '--soft', base_sha)
        if g('diff', '--cached', '--quiet').returncode != 0:
            ci = g('commit', '-m', f'{tid}: automated dispatch')
            if ci.returncode != 0:
                # A failed commit would leave `branch` pointing at base_ref, so
                # the subsequent push reports "up-to-date" (exit 0) and the empty
                # branch would be reported as a successful run and silently merged
                # as a no-op. Fail explicitly instead.
                return {'exit_code': 1,
                        'error': f'git commit failed: {ci.stderr.strip()}',
                        'log_path': log_path}
        # "No staged changes" does NOT mean "nothing happened": in autopilot mode
        # copilot has git access and may commit the work itself (always, when the
        # prompt asks it to, and sometimes on its own). That leaves a clean index
        # but a branch legitimately ahead of base. Only treat the run as empty
        # when the branch did not advance beyond the base it was cut from —
        # otherwise we'd drop copilot's own commit(s) and report a false failure.
        head = g('rev-parse', 'HEAD').stdout.strip()
        if not head or head == base_sha:
            # I3: a probe (read-only/diagnostic) task legitimately produces no
            # commit — that is SUCCESS, not failure. Return exit_code=0 so
            # complete_task moves it to the terminal 'probed' state instead of
            # requeuing it (and repaying the fresh-clone cost) up to max_attempts.
            if task.get('kind') == 'probe':
                return {'exit_code': 0, 'error': None, 'log_path': log_path}
            return {'exit_code': 1, 'error': 'no changes produced', 'log_path': log_path}

        # A resume collapse rewrote branch history (WIP commits -> one commit that
        # is not their descendant), so the push must overwrite the remote WIP.
        # --force-with-lease is safe: the hive solely owns this task branch.
        push_args = ['push']
        if resume:
            push_args.append('--force-with-lease')
        push_args += ['origin', branch]
        push = g(*push_args)
        if push.returncode != 0:
            return {'exit_code': 1, 'error': f'push failed: {push.stderr.strip()}',
                    'log_path': log_path}

        commit = g('rev-parse', 'HEAD').stdout.strip()
        # Sc1 (#10): run the build/test gate here (parallel across workers). The
        # merge loop trusts this result when the base hasn't drifted, otherwise it
        # re-verifies the rebased tree. verified_base lets it detect that drift.
        ran, vok, vsummary = _worker_verify(run_dir)
        result = {'exit_code': proc.returncode, 'commit_hash': commit,
                  'log_path': log_path, 'verified_base': base_sha}
        if ran:
            result['verified'] = vok
            log.info('worker verify %s for %s: %s',
                     'passed' if vok else 'FAILED', tid, vsummary)
        # Extract copilot billing usage from the JSONL log (best-effort; never
        # breaks the task).
        try:
            usage = _parse_usage_from_log(log_path)
            if usage is not None:
                result['usage_json'] = json.dumps(usage)
        except Exception:
            pass
        return result
    except subprocess.TimeoutExpired:
        # Preserve partial work so the next attempt resumes instead of restarting.
        wip = _wip_commit_push(g, branch, tid, task.get('attempts'))
        err = 'copilot timed out' + (f'; {wip}' if wip else '; no partial work to preserve')
        return {'exit_code': 124, 'error': err, 'log_path': log_path}
    except Exception as e:
        return {'exit_code': 1, 'error': str(e), 'log_path': log_path}
    finally:
        try:
            os.remove(prompt_path)
        except OSError:
            pass
        # Reclaim the per-task clone. Without this every task leaves a full
        # working tree + .git behind under WORK_DIR (on the least-priv Windows
        # worker that is C:\hive\work), eventually filling the volume until
        # `git clone` fails and the worker can no longer complete any task.
        shutil.rmtree(run_dir, ignore_errors=True)


# ── Version-driven self-update (worker-native; no copilot/LLM in the loop) ────

UPDATE_EXIT_CODE = 42   # nonzero => WinSW onfailure / systemd Restart respawns us
UPDATE_FILES = ('worker_loop.py', 'hive_caps.py', 'hive_queue.py',
                'hive_log.py', 'hive_version.py')


def _auto_update_enabled() -> bool:
    return os.environ.get('HIVE_AUTO_UPDATE', '').strip().lower() in (
        '1', 'true', 'yes', 'on')


def _self_update(client: QueueClient, target: int, reason: str) -> None:
    """Download -> verify (sha256) -> compile-check -> back up -> swap the worker
    code, then exit nonzero so the service manager restarts us into it. Aborts
    (returns) without touching disk on any integrity/compile failure, so a bad
    push can never brick a worker on restart. Called ONLY when idle."""
    hive_dir = os.path.dirname(os.path.abspath(__file__))
    log.warning('self-update: v%s -> v%s (%s); fetching manifest',
                CODE_VERSION, target, reason)
    manifest = client.get_version()
    files = manifest.get('files') or {}
    server_ver = manifest.get('version')
    if 'worker_loop.py' not in files:
        log.error('self-update ABORT: manifest has no worker_loop.py'); return

    staged: dict[str, bytes] = {}
    for name, want in files.items():
        if name not in UPDATE_FILES:
            continue
        data = client.download(name)
        got = hashlib.sha256(data).hexdigest()
        if got != want:
            log.error('self-update ABORT: %s hash mismatch (want %s got %s)',
                      name, want[:12], got[:12])
            return
        if name.endswith('.py'):
            try:
                compile(data, name, 'exec')
            except SyntaxError as e:
                log.error('self-update ABORT: %s does not compile: %s', name, e)
                return
        staged[name] = data

    ts = time.strftime('%Y%m%d-%H%M%S')
    for name, data in staged.items():
        path = os.path.join(hive_dir, name)
        if os.path.exists(path):
            try:
                shutil.copy2(path, f'{path}.bak-{ts}')
            except OSError:
                pass
        with open(path, 'wb') as f:
            f.write(data)
    log.warning('self-update: swapped %d files -> v%s (backup .bak-%s); '
                'exiting %d for service restart',
                len(staged), server_ver, ts, UPDATE_EXIT_CODE)
    logging_flush = getattr(hive_log, 'flush', None)
    if callable(logging_flush):
        logging_flush()
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(UPDATE_EXIT_CODE)


def _maybe_self_update(client: QueueClient) -> None:
    """Decide whether to self-update based on the last /claim's version signal.
    mode A (command): update_to set and != our version -> update (even re-pin).
    mode B (auto): HIVE_AUTO_UPDATE=1 and advertised version is newer -> update.
    Exits the process on success; on failure logs and stays on the current code."""
    cmd = client.update_to
    srv = client.server_code_version
    if cmd is not None and cmd != CODE_VERSION:
        target, reason = cmd, 'commanded'
    elif _auto_update_enabled() and srv is not None and srv > CODE_VERSION:
        target, reason = srv, 'auto (version bump)'
    else:
        return
    try:
        _self_update(client, target, reason)
    except SystemExit:
        raise
    except Exception as e:
        log.error('self-update failed (staying on v%s): %s', CODE_VERSION, e)


# ── Main loop ─────────────────────────────────────────────────────────────────

def loop(worker_id: str, lane: str | None, client: QueueClient, lab_git: str,
         caps: list[str] | None = None):
    caps = caps or []
    log.info('worker %s up: lane=%s caps=%s code=v%s — polling %s',
             worker_id, lane, sorted(caps), CODE_VERSION, client.base)
    while True:
        try:
            task = client.claim(worker_id, lane, caps)
        except Exception as e:
            log.warning('worker %s claim error: %s; backing off', worker_id, e)
            time.sleep(POLL_BACKOFF_S)
            continue

        if task is None:
            # Idle: safe point to apply a pending self-update (never mid-task).
            _maybe_self_update(client)
            continue  # long-poll already waited server-side

        # A *commanded* update (operator `halctl update`) must apply even when the
        # queue never drains to idle — otherwise a continuously-busy worker would
        # run every task on stale code and never pick up the new version. Apply it
        # BEFORE processing this freshly-claimed task: _maybe_self_update exits the
        # process on success, so this claim's lease expires and the reaper re-queues
        # it for the restarted (new-version) worker. Best-effort: if the update
        # fails it logs and we fall through to run the task on the current code.
        if client.update_to is not None and client.update_to != CODE_VERSION:
            log.warning('worker %s: commanded update v%s->v%s pending; applying '
                        'before running %s (claim will be re-queued)',
                        worker_id, CODE_VERSION, client.update_to, task['id'])
            _maybe_self_update(client)

        tid = task['id']
        ltok = task.get('lease_token')
        kind = task.get('kind', 'work')
        log.info('worker %s claimed %s (kind=%s attempt %s)',
                 worker_id, tid, kind, task['attempts'])
        t0 = time.time()

        if kind == 'review':
            prog = _ProgressHolder()
            with Heartbeat(client, tid, worker_id, ltok, progress=prog) as hb:
                result = run_review_task(task, worker_id, lab_git, progress=prog)
                if hb.abandon.is_set():
                    log.warning('worker %s: %s abandoned (reaped); not reporting',
                                worker_id, tid)
                    continue
            # Ship nothing through /complete: the /review endpoint closes the
            # review task server-side. Clean up the worker-local log.
            log_path = result.get('log_path')
            if log_path and os.path.exists(log_path):
                try:
                    os.remove(log_path)
                except OSError:
                    pass
            try:
                res = client.post_review(task.get('review_of'), result['verdict'],
                                         result.get('findings', ''), tid,
                                         usage_json=result.get('usage_json'))
            except Exception as e:
                log.warning('worker %s: failed to post review verdict for %s: %s',
                            worker_id, tid, e)
                res = 'post-failed'
            dt = time.time() - t0
            log.info('worker %s: review %s -> verdict=%s result=%s (%.1fs)',
                     worker_id, tid, result['verdict'], res, dt)
            continue

        prog = _ProgressHolder()
        with Heartbeat(client, tid, worker_id, ltok, progress=prog) as hb:
            result = run_task(task, worker_id, lab_git, progress=prog)
            if hb.abandon.is_set():
                log.warning('worker %s: %s abandoned (reaped); not reporting',
                            worker_id, tid)
                continue

        # Ship the copilot log tail back so failures are triageable from HAL
        # (U2 #16), then delete the worker-local temp log to avoid leaking it.
        log_path = result.get('log_path')
        log_tail = _read_log_tail(log_path)
        if log_path and os.path.exists(log_path):
            try:
                os.remove(log_path)
            except OSError:
                pass

        status = client.complete(
            task_id=tid, worker_id=worker_id, exit_code=result.get('exit_code', 1),
            commit_hash=result.get('commit_hash'), log_path=result.get('log_path'),
            log_tail=log_tail, verified=result.get('verified'),
            verified_base=result.get('verified_base'), error=result.get('error'),
            lease_token=ltok, usage_json=result.get('usage_json'),
        )
        dt = time.time() - t0
        log.info('worker %s: %s -> %s (%.1fs) %s', worker_id, tid, status, dt,
                 result.get('error') or result.get('commit_hash', ''))


def _default_caps(lane: str | None) -> list[str]:
    """Capabilities every worker advertises automatically: OS + platform + lane
    name PLUS self-probed toolset (build:dotnet, has:docker, role:builder|tester,
    ...). Extra caps still addable via --cap / HIVE_CAPS. Probing is which()-only
    so it's cheap and safe at every startup."""
    import hive_caps
    return sorted(hive_caps.probe_caps(lane))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--worker-id', required=True)
    ap.add_argument('--lane', default=None, help="pinned lane; omit to take any task")
    ap.add_argument('--cap', action='append', default=[],
                    help='advertise an extra capability tag (repeatable), '
                         'e.g. --cap msbuild --cap dotnet')
    ap.add_argument('--queue', default=os.environ.get('HIVE_QUEUE_URL',
                                                       'http://127.0.0.1:5001'))
    ap.add_argument('--token', default=os.environ.get('HIVE_TOKEN'))
    ap.add_argument('--lab-git', default=os.environ.get('HIVE_LAB_GIT', q.LAB_GIT))
    args = ap.parse_args()

    token = args.token
    if not token:
        tf = os.path.expanduser('~/.hive/worker.token')
        if os.path.exists(tf):
            token = open(tf).read().strip()
    if not token:
        ap.error('no token: set --token, $HIVE_TOKEN, or ~/.hive/worker.token')

    # Merge auto-detected caps + --cap flags + HIVE_CAPS env (comma/space list).
    caps = set(_default_caps(args.lane)) | {c.lower() for c in args.cap}
    env_caps = os.environ.get('HIVE_CAPS', '')
    caps |= {c.strip().lower() for c in re.split(r'[\s,]+', env_caps) if c.strip()}

    client = QueueClient(args.queue, token)
    loop(args.worker_id, args.lane, client, args.lab_git, caps=sorted(caps))


if __name__ == '__main__':
    main()
