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
import subprocess
import tempfile
import threading
import time

import requests

import hive_queue as q   # only used for LAB_GIT path + local git; DB via REST

HEARTBEAT_INTERVAL_S = 60
POLL_BACKOFF_S       = 3         # sleep when /claim returns empty (204)
COPILOT_TIMEOUT_S    = 840


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
    argv = [exe, '-p', prompt_ref, '--no-ask-user',
            '--allow-all', '--autopilot', '--output-format', 'json']
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

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        r = requests.post(f'{self.base}/heartbeat',
                          json={'task_id': task_id, 'worker_id': worker_id},
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
    def __init__(self, client: QueueClient, task_id: str, worker_id: str):
        self.client, self.task_id, self.worker_id = client, task_id, worker_id
        self.abandon = threading.Event()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            try:
                if not self.client.heartbeat(self.task_id, self.worker_id):
                    print(f'  [hb] lease lost for {self.task_id} — abandoning')
                    self.abandon.set()
                    return
            except Exception as e:
                print(f'  [hb] error: {e}')

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()


# ── The run ───────────────────────────────────────────────────────────────────

def run_task(task: dict, worker_id: str, lab_git: str) -> dict:
    """
    Clone lab.git, branch, run copilot headless, commit, push. Returns dict with
    exit_code, commit_hash, log_path, error.
    """
    branch, base_ref, tid = task['branch'], task['base_ref'], task['id']
    run_dir = tempfile.mkdtemp(prefix=f'{tid}-')
    # Log lives OUTSIDE the clone so `git add -A` never commits it (a log inside
    # run_dir pollutes every commit and causes cross-task rebase conflicts).
    log_fd, log_path = tempfile.mkstemp(prefix=f'{tid}-', suffix='.jsonl')
    os.close(log_fd)
    # Prompt also lives outside the clone and is fed to copilot via `-p @file`
    # (see _copilot_argv): a large embedded prompt overflows the Windows argv
    # limit (WinError 206). Writing it inside run_dir would also get committed.
    prompt_fd, prompt_path = tempfile.mkstemp(prefix=f'{tid}-', suffix='.prompt')
    with os.fdopen(prompt_fd, 'w', encoding='utf-8') as pf:
        pf.write(task['prompt'])

    def g(*a, cwd=run_dir):
        return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True)

    try:
        cl = subprocess.run(['git', 'clone', lab_git, run_dir],
                            capture_output=True, text=True)
        if cl.returncode != 0:
            return {'exit_code': 1, 'error': f'clone failed: {cl.stderr.strip()}'}

        # base_ref may not exist yet on a fresh repo — branch from HEAD if so.
        if g('rev-parse', '--verify', f'origin/{base_ref}').returncode == 0:
            g('checkout', base_ref)
        g('checkout', '-b', branch)
        g('config', 'user.email', 'hal@lab')
        g('config', 'user.name', f'HAL worker {worker_id}')

        with open(log_path, 'w') as log:
            proc = subprocess.run(
                _copilot_argv(f'@{prompt_path}', model=task.get('model')),
                cwd=run_dir, stdout=log, stderr=subprocess.STDOUT,
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
        return {'exit_code': proc.returncode, 'commit_hash': commit,
                'log_path': log_path}
    except subprocess.TimeoutExpired:
        return {'exit_code': 124, 'error': 'copilot timed out', 'log_path': log_path}
    except Exception as e:
        return {'exit_code': 1, 'error': str(e), 'log_path': log_path}
    finally:
        try:
            os.remove(prompt_path)
        except OSError:
            pass


# ── Main loop ─────────────────────────────────────────────────────────────────

def loop(worker_id: str, lane: str | None, client: QueueClient, lab_git: str,
         caps: list[str] | None = None):
    caps = caps or []
    print(f'[worker {worker_id}] lane={lane} caps={sorted(caps)} — polling {client.base}')
    while True:
        try:
            task = client.claim(worker_id, lane, caps)
        except Exception as e:
            print(f'[worker {worker_id}] claim error: {e}; backing off')
            time.sleep(POLL_BACKOFF_S)
            continue

        if task is None:
            continue  # long-poll already waited server-side

        tid = task['id']
        print(f'[worker {worker_id}] claimed {tid} (attempt {task["attempts"]})')
        t0 = time.time()
        with Heartbeat(client, tid, worker_id) as hb:
            result = run_task(task, worker_id, lab_git)
            if hb.abandon.is_set():
                print(f'[worker {worker_id}] {tid} abandoned (reaped); not reporting')
                continue

        status = client.complete(
            task_id=tid, worker_id=worker_id, exit_code=result.get('exit_code', 1),
            commit_hash=result.get('commit_hash'), log_path=result.get('log_path'),
            error=result.get('error'),
        )
        dt = time.time() - t0
        print(f'[worker {worker_id}] {tid} -> {status} ({dt:.1f}s) '
              f'{result.get("error") or result.get("commit_hash","")}')


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
