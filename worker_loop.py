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
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time

import requests

import hive_queue as q   # only used for LAB_GIT path + local git; DB via REST
import hive_log

log = hive_log.get_logger('hive.worker')

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

HEARTBEAT_INTERVAL_S = 60
POLL_BACKOFF_S       = 3         # sleep when /claim returns empty (204)
COPILOT_TIMEOUT_S    = 840

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
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip('/')
        self.h = {'Authorization': f'Bearer {token}'}

    def claim(self, worker_id: str, lane: str | None, caps: list[str] | None = None):
        r = requests.post(f'{self.base}/claim',
                          json={'worker_id': worker_id, 'lane': lane, 'caps': caps},
                          headers=self.h, timeout=40)
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    def heartbeat(self, task_id: str, worker_id: str,
                  lease_token: str | None = None) -> bool:
        r = requests.post(f'{self.base}/heartbeat',
                          json={'task_id': task_id, 'worker_id': worker_id,
                                'lease_token': lease_token},
                          headers=self.h, timeout=15)
        r.raise_for_status()
        return r.json().get('alive', False)

    def complete(self, **kw) -> str:
        r = requests.post(f'{self.base}/complete', json=kw, headers=self.h, timeout=30)
        r.raise_for_status()
        return r.json().get('status')


# ── Heartbeat thread ──────────────────────────────────────────────────────────

class Heartbeat:
    """Renews the lease every HEARTBEAT_INTERVAL_S; signals abandon if reaped."""
    def __init__(self, client: QueueClient, task_id: str, worker_id: str,
                 lease_token: str | None = None):
        self.client, self.task_id, self.worker_id = client, task_id, worker_id
        self.lease_token = lease_token
        self.abandon = threading.Event()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            try:
                if not self.client.heartbeat(self.task_id, self.worker_id,
                                             self.lease_token):
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
    """Inject the shared hive token as an HTTP header for smart-HTTP git ops.

    The lab.git transport (git_http_server.py) now requires token auth. We pass
    the token per-invocation via `-c http.extraheader=...` rather than embedding
    it in the remote URL, so it never lands in .git/config on the throwaway clone.
    HIVE_TOKEN is already exported to every worker service (WinSW/systemd/launchd).
    """
    tok = (os.environ.get('HIVE_GIT_TOKEN') or os.environ.get('HIVE_TOKEN') or '').strip()
    if tok:
        return ['-c', f'http.extraheader=Authorization: Bearer {tok}']
    return []


def run_task(task: dict, worker_id: str, lab_git: str) -> dict:
    """
    Clone lab.git, branch, run copilot headless, commit, push. Returns dict with
    exit_code, commit_hash, log_path, error.
    """
    branch, base_ref, tid = task['branch'], task['base_ref'], task['id']
    run_dir = tempfile.mkdtemp(prefix=f'{tid}-', dir=WORK_DIR)
    # Log lives OUTSIDE the clone so `git add -A` never commits it (a log inside
    # run_dir pollutes every commit and causes cross-task rebase conflicts).
    log_fd, log_path = tempfile.mkstemp(prefix=f'{tid}-', suffix='.jsonl', dir=WORK_DIR)
    os.close(log_fd)
    # Prompt also lives outside the clone and is fed to copilot via `-p @file`
    # (see _copilot_argv): a large embedded prompt overflows the Windows argv
    # limit (WinError 206). Writing it inside run_dir would also get committed.
    prompt_fd, prompt_path = tempfile.mkstemp(prefix=f'{tid}-', suffix='.prompt', dir=WORK_DIR)
    with os.fdopen(prompt_fd, 'w', encoding='utf-8') as pf:
        pf.write(task['prompt'])

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
        clone_filter = os.environ.get('HIVE_CLONE_FILTER', 'blob:none').strip()
        clone_args = ['clone']
        if clone_filter:
            clone_args.append(f'--filter={clone_filter}')
        clone_args += [lab_git, run_dir]
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
        g('checkout', '-b', branch)
        g('config', 'user.email', 'hal@lab')
        g('config', 'user.name', f'HAL worker {worker_id}')

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
        if g('diff', '--cached', '--quiet').returncode != 0:
            g('commit', '-m', f'{tid}: automated dispatch')
        else:
            return {'exit_code': 1, 'error': 'no changes produced', 'log_path': log_path}

        push = g('push', 'origin', branch)
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
        return result
    except subprocess.TimeoutExpired:
        return {'exit_code': 124, 'error': 'copilot timed out', 'log_path': log_path}
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


# ── Main loop ─────────────────────────────────────────────────────────────────

def loop(worker_id: str, lane: str | None, client: QueueClient, lab_git: str,
         caps: list[str] | None = None):
    caps = caps or []
    log.info('worker %s up: lane=%s caps=%s — polling %s',
             worker_id, lane, sorted(caps), client.base)
    while True:
        try:
            task = client.claim(worker_id, lane, caps)
        except Exception as e:
            log.warning('worker %s claim error: %s; backing off', worker_id, e)
            time.sleep(POLL_BACKOFF_S)
            continue

        if task is None:
            continue  # long-poll already waited server-side

        tid = task['id']
        ltok = task.get('lease_token')
        log.info('worker %s claimed %s (attempt %s)', worker_id, tid, task['attempts'])
        t0 = time.time()
        with Heartbeat(client, tid, worker_id, ltok) as hb:
            result = run_task(task, worker_id, lab_git)
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
            lease_token=ltok,
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
