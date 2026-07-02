#!/usr/bin/env python3
"""
HAL — DAG Dispatcher (Phase 4+)
Dispatches coding tasks to a fleet of Copilot CLI workers over WinRM,
merges results into ~/hive/lab.git, respecting DAG dependencies.

Workers:
  hal         — local lane (subprocess)
  ar1         — 192.168.100.11, WinRM/Kerberos (domain-joined)
  memberSrv   — 192.168.100.20, WinRM/NTLM (workgroup)

Usage:
  python3 dispatch_dag.py                  # run default TASKS list
  python3 dispatch_dag.py fleet            # alias for above
  python3 dispatch_dag.py single <task_id> # dispatch one task
"""

import os
import sys
import time
import subprocess
import threading
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional
import winrm

# ── Config ────────────────────────────────────────────────────────────────────

LAB_GIT      = os.path.expanduser('~/hive/lab.git')
LOCAL_CWD    = os.path.expanduser('~/hive/workspace')
WORKER_BASE  = r'C:\hive\runs'
PS_WRAPPER   = r'C:\Users\a\.hive\hive-copilot.ps1'


def _lab_pass():
    p = os.environ.get('LAB_PASS')
    if not p:
        raise SystemExit('LAB_PASS not set — source ~/.hive/hive.env')
    return p

WORKERS = {
    'ar1': {
        'host':      '192.168.100.11',
        'transport': 'kerberos',
        'username':  r'fodor\a',
        'password':  _lab_pass(),
    },
    'memberSrv': {
        'host':      '192.168.100.20',
        'transport': 'ntlm',
        'username':  r'memberSrv\a',
        'password':  _lab_pass(),
    },
}

COPILOT_FLAGS = '--no-ask-user --allow-all-tools --autopilot -m claude-haiku-4.5'

_merge_lock = threading.Lock()

# ── Task definition ───────────────────────────────────────────────────────────

@dataclass
class Task:
    id:      str
    prompt:  str
    branch:  str        = field(default='')
    deps:    list[str]  = field(default_factory=list)
    worker:  str        = 'ar1'   # 'hal' | 'ar1' | 'memberSrv'
    model:   Optional[str] = None

    def __post_init__(self):
        if not self.branch:
            self.branch = f'feature/{self.id}'


# ── Define your task DAG here ─────────────────────────────────────────────────

TASKS: list[Task] = [
    Task(
        id='task-001',
        prompt='Create a Python module math_utils.py with add(a,b) and mul(a,b) functions.',
        worker='ar1',
    ),
    Task(
        id='task-002',
        prompt='Write pytest tests for math_utils.py (import it). Cover add and mul with positive, negative, and zero inputs.',
        deps=['task-001'],
        worker='ar1',
    ),
]


# ── WinRM dispatch ────────────────────────────────────────────────────────────

def _winrm_session(worker_name: str) -> winrm.Session:
    cfg = WORKERS[worker_name]
    return winrm.Session(
        f"http://{cfg['host']}:5985/wsman",
        auth=(cfg['username'], cfg['password']),
        transport=cfg['transport'],
        read_timeout_sec=900,
        operation_timeout_sec=840,
    )


def _encode_cmd(ps_script: str) -> str:
    """Base64-encode a PowerShell script for -EncodedCommand."""
    import base64
    return base64.b64encode(ps_script.encode('utf-16-le')).decode('ascii')


def dispatch_winrm(task: Task) -> dict:
    """Run task on a Windows WinRM worker. Returns {ok, output, elapsed}."""
    run_dir = f"{WORKER_BASE}\\{task.id}"
    prompt_escaped = task.prompt.replace("'", "''")
    model_flag = f'-m {task.model}' if task.model else ''

    ps = textwrap.dedent(f"""\
        $ErrorActionPreference = 'Stop'
        $runDir = '{run_dir}'
        New-Item -ItemType Directory -Force -Path $runDir | Out-Null
        Set-Location $runDir

        # Clone from HAL bare repo
        git clone file:////{os.environ.get('HOSTNAME','HAL')}/home/afodor/hive/lab.git . 2>&1
        git checkout -b '{task.branch}' 2>&1

        # Dispatch to Copilot CLI
        & '{PS_WRAPPER}' -Prompt '{prompt_escaped}' {COPILOT_FLAGS} {model_flag} > output.jsonl 2>&1

        # Push result
        git add -A
        git diff --cached --quiet || git commit -m '{task.id}: automated dispatch'
        git push origin '{task.branch}' 2>&1

        Write-Output "DISPATCH_OK:{task.id}"
    """)

    t0 = time.time()
    session = _winrm_session(task.worker)
    result = session.run_ps(ps)
    elapsed = time.time() - t0

    stdout = result.std_out.decode('utf-8', errors='replace')
    stderr = result.std_err.decode('utf-8', errors='replace')
    ok = result.status_code == 0 and f'DISPATCH_OK:{task.id}' in stdout

    return {'ok': ok, 'stdout': stdout, 'stderr': stderr, 'elapsed': elapsed}


def dispatch_local(task: Task) -> dict:
    """Run task locally as a subprocess (HAL lane)."""
    run_dir = os.path.expanduser(f'~/hive/runs/{task.id}')
    os.makedirs(run_dir, exist_ok=True)
    t0 = time.time()

    model_flag = ['-m', task.model] if task.model else []
    try:
        subprocess.run(['git', 'clone', LAB_GIT, run_dir], check=True, capture_output=True)
        subprocess.run(['git', 'checkout', '-b', task.branch], cwd=run_dir, check=True, capture_output=True)

        result = subprocess.run(
            ['gh', 'copilot', '-p', task.prompt, '--no-ask-user',
             '--allow-all-tools', '--autopilot'] + model_flag,
            cwd=run_dir, capture_output=True, text=True, timeout=840,
        )

        subprocess.run(['git', 'add', '-A'], cwd=run_dir, check=True, capture_output=True)
        diff = subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=run_dir, capture_output=True)
        if diff.returncode != 0:
            subprocess.run(['git', 'commit', '-m', f'{task.id}: automated dispatch'],
                           cwd=run_dir, check=True, capture_output=True)
        subprocess.run(['git', 'push', 'origin', task.branch],
                       cwd=run_dir, check=True, capture_output=True)

        ok = result.returncode == 0
        return {'ok': ok, 'stdout': result.stdout, 'stderr': result.stderr,
                'elapsed': time.time() - t0}
    except Exception as e:
        return {'ok': False, 'stdout': '', 'stderr': str(e), 'elapsed': time.time() - t0}


# ── Git merge ─────────────────────────────────────────────────────────────────

def merge_branch(task: Task) -> bool:
    """Fast-forward merge task branch into lab.git/main. Serialised."""
    with _merge_lock:
        try:
            subprocess.run(
                ['git', '--git-dir', LAB_GIT, 'merge', '--ff-only', f'origin/{task.branch}'],
                check=True, capture_output=True,
            )
            print(f'  ✅ merged {task.branch} → main')
            return True
        except subprocess.CalledProcessError as e:
            # Try non-fast-forward merge if ff fails
            try:
                subprocess.run(
                    ['git', '--git-dir', LAB_GIT, 'merge', '--no-ff',
                     '-m', f'Merge {task.branch}', task.branch],
                    check=True, capture_output=True,
                )
                print(f'  ✅ merged {task.branch} → main (no-ff)')
                return True
            except subprocess.CalledProcessError as e2:
                print(f'  ❌ merge failed for {task.branch}: {e2.stderr.decode()}')
                return False


# ── DAG execution ─────────────────────────────────────────────────────────────

def run_task(task: Task, completed: set[str]) -> dict:
    print(f'\n[{task.id}] dispatching → {task.worker}  branch={task.branch}')
    if task.worker == 'hal':
        result = dispatch_local(task)
    else:
        result = dispatch_winrm(task)

    status = '✅' if result['ok'] else '❌'
    print(f'[{task.id}] {status} {result["elapsed"]:.1f}s')
    if not result['ok']:
        print(f'  stderr: {result["stderr"][:500]}')
        return result

    merged = merge_branch(task)
    result['merged'] = merged
    if merged:
        completed.add(task.id)
    return result


def deps_satisfied(task: Task, completed: set[str]) -> bool:
    return all(d in completed for d in task.deps)


def run_dag(tasks: list[Task]) -> None:
    completed: set[str] = set()
    pending = list(tasks)
    in_flight: dict[str, threading.Future] = {}
    results: dict[str, dict] = {}
    t0 = time.time()

    print(f'\n{"═"*60}')
    print(f'HAL DAG Dispatcher — {len(tasks)} tasks')
    print(f'{"═"*60}')

    with ThreadPoolExecutor(max_workers=len(WORKERS) + 1) as pool:
        while pending or in_flight:
            # Launch tasks whose deps are satisfied and no worker conflict
            busy_workers = {fut_meta['worker'] for fut_meta in
                            [{'worker': t.worker} for t in pending
                             if t.id in in_flight]}

            for task in list(pending):
                if task.id in in_flight:
                    continue
                if not deps_satisfied(task, completed):
                    continue
                if task.worker in {t.worker for t in pending if t.id in in_flight}:
                    continue
                print(f'  → queuing {task.id} on {task.worker}')
                fut = pool.submit(run_task, task, completed)
                in_flight[task.id] = fut
                pending.remove(task)

            # Collect done futures
            done_ids = [tid for tid, fut in in_flight.items() if fut.done()]
            for tid in done_ids:
                results[tid] = in_flight.pop(tid).result()

            if not done_ids and in_flight:
                time.sleep(1)

    wall = time.time() - t0
    print(f'\n{"═"*60}')
    print(f'Done — wall clock {wall:.1f}s')
    for t in tasks:
        r = results.get(t.id, {})
        ok = '✅' if r.get('ok') else '❌'
        elapsed = r.get('elapsed', 0)
        print(f'  {ok} {t.id:12s} {elapsed:6.1f}s  {t.worker}')
    print(f'{"═"*60}\n')


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'fleet'

    if mode in ('fleet', 'dag'):
        run_dag(TASKS)
    elif mode == 'single' and len(sys.argv) > 2:
        tid = sys.argv[2]
        task = next((t for t in TASKS if t.id == tid), None)
        if not task:
            print(f'Unknown task: {tid}')
            sys.exit(1)
        result = run_task(task, set())
        sys.exit(0 if result.get('ok') else 1)
    else:
        print(__doc__)
        sys.exit(1)
