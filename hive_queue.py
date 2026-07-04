#!/usr/bin/env python3
"""
HAL — Live Hive Queue core (Phase 9.1)

The correctness core of the pull-persistent hive:
  - queue.db schema v2 + connection invariant (WAL, busy_timeout, FK)   [C4/C2]
  - atomic claim() with dependency-gate + lease                         [C1/C3]
  - heartbeat + reaper (crashed-worker recovery, dead-letter)           [C3/M1]
  - merge_loop: rebase -> verify -> ff-merge, park needs_attention      [C5/M2]

All DB access happens on HAL's local filesystem. Windows workers NEVER open the
file directly (SQLite locking is broken over SMB/NFS) — they reach the queue only
through queue_server.py. HAL is therefore the single clock source: every timestamp
is written by datetime('now') on HAL.

Design source: gist hal-phase9.1-hardening.md.
"""

from __future__ import annotations

import os
import random
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

import hive_log

log = hive_log.get_logger('hive.queue')

# ── Config ────────────────────────────────────────────────────────────────────

HIVE_DIR   = os.path.expanduser(os.environ.get('HIVE_DIR', '~/hive'))
QUEUE_DB   = os.environ.get('HIVE_QUEUE_DB', os.path.join(HIVE_DIR, 'queue.db'))
LAB_GIT    = os.environ.get('HIVE_LAB_DIR', os.path.join(HIVE_DIR, 'lab.git'))
DEFAULT_BASE_REF = 'master'          # lab.git default branch

LEASE_MINUTES     = 15               # §3 claim lease window
HEARTBEAT_RENEW   = '+15 minutes'    # §4 pulse renews the lease
REAPER_INTERVAL_S = 30               # §4 reaper cadence
MERGE_INTERVAL_S  = 2                # §7 merge-loop cadence
VERIFY_LEASE      = '+30 minutes'    # §7 max time a task may sit in 'verifying'

SCHEMA_VERSION = 8
WORKER_STALE_S = 120                  # a worker not seen within this is "offline"

# Statuses considered "in flight" (hold a lease) for the reaper.
_INFLIGHT = ('claimed', 'running')


# ── Connection invariant (fixes C4) ───────────────────────────────────────────

def connect(db_path: str = QUEUE_DB) -> sqlite3.Connection:
    """Open queue.db with the mandatory PRAGMAs. Every process MUST use this."""
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode = WAL;')      # concurrent readers + 1 writer
    conn.execute('PRAGMA busy_timeout = 5000;')     # wait 5s for the lock, don't throw
    conn.execute('PRAGMA foreign_keys = ON;')       # enforce task_deps integrity
    conn.execute('PRAGMA synchronous = NORMAL;')    # WAL-safe, fast enough for a lab
    return conn


# Sc3 (#12): busy_timeout makes SQLite *wait* for a lock, but under sustained
# write contention (every worker long-polling /claim + the merge loop + reaper)
# the wait can still expire and raise "database is locked", which surfaced as a
# 500 on /claim. Retry the whole transaction a few times with jittered backoff so
# a transient lock degrades to a slightly slower claim instead of an error.
BUSY_RETRIES = 6
_BUSY_BASE_S = 0.05


def _is_locked_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and \
        ('locked' in str(exc).lower() or 'busy' in str(exc).lower())


def with_retry(fn, *args, retries: int = BUSY_RETRIES, **kwargs):
    """Run fn(*args, **kwargs), retrying on a transient SQLite lock. Any other
    error (or exhausting retries) propagates unchanged."""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if not _is_locked_error(exc) or attempt == retries - 1:
                raise
            time.sleep(_BUSY_BASE_S * (2 ** attempt) + random.uniform(0, _BUSY_BASE_S))


@contextmanager
def cursor(db_path: str = QUEUE_DB) -> Iterator[sqlite3.Cursor]:
    conn = connect(db_path)
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


# ── Schema (fixes C2) ──────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    branch        TEXT NOT NULL UNIQUE,
    base_ref      TEXT NOT NULL DEFAULT 'master',
    prompt        TEXT NOT NULL,
    worker        TEXT,                        -- exact lane pin ('ar1'|'hal'|...), NULL = any
    requires      TEXT,                        -- capability tags a worker MUST have (space-sep), NULL = none
    scope         TEXT,                        -- optional build/test scope for verify gate
    priority      INTEGER NOT NULL DEFAULT 5,  -- 1=urgent .. 10=background
    complexity    INTEGER,                     -- 1=simple 2=standard 3=complex -> model tier
    model         TEXT,                        -- copilot model derived from complexity
    status        TEXT NOT NULL DEFAULT 'pending',
    worker_id     TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    lease_expires DATETIME,
    heartbeat_at  DATETIME,
    not_before    DATETIME,                    -- retry backoff gate
    created_at    DATETIME NOT NULL DEFAULT (datetime('now')),
    claimed_at    DATETIME,
    started_at    DATETIME,
    done_at       DATETIME,
    commit_hash   TEXT,
    exit_code     INTEGER,
    turns         INTEGER,
    cost_pr       REAL,
    log_path      TEXT,
    log_tail      TEXT,                        -- last KBs of the worker copilot log (U2 #16)
    verified      INTEGER,                      -- worker-side verify (Sc1 #10): NULL=not run, 0=failed, 1=passed
    verified_base TEXT,                         -- base_ref sha the worker verified against (drift check)
    error         TEXT
);

CREATE TABLE IF NOT EXISTS task_deps (
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on)
);

CREATE INDEX IF NOT EXISTS idx_tasks_claimable
    ON tasks(status, priority, created_at);

CREATE TABLE IF NOT EXISTS workers (
    worker_id    TEXT PRIMARY KEY,
    lane         TEXT,
    caps         TEXT,                          -- space-separated capability tags
    host         TEXT,                          -- remote address as seen by the server
    status       TEXT NOT NULL DEFAULT 'idle',  -- idle | busy
    current_task TEXT,                          -- task_id when busy, else NULL
    first_seen   DATETIME NOT NULL DEFAULT (datetime('now')),
    last_seen    DATETIME NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db(db_path: str = QUEUE_DB) -> None:
    """Create/upgrade the schema. Idempotent, with in-place migration."""
    with cursor(db_path) as cur:
        cur.executescript(_SCHEMA)
        # Migration v2 -> v3: add capability-requirement column to existing DBs.
        cols = {r['name'] for r in cur.execute('PRAGMA table_info(tasks)').fetchall()}
        if 'requires' not in cols:
            cur.execute('ALTER TABLE tasks ADD COLUMN requires TEXT')
        # Migration v4 -> v5: per-task complexity tier + derived model.
        if 'complexity' not in cols:
            cur.execute('ALTER TABLE tasks ADD COLUMN complexity INTEGER')
        if 'model' not in cols:
            cur.execute('ALTER TABLE tasks ADD COLUMN model TEXT')
        # Migration v5 -> v6: store the tail of the worker copilot log so failure
        # triage no longer requires SSHing the worker (U2 #16).
        if 'log_tail' not in cols:
            cur.execute('ALTER TABLE tasks ADD COLUMN log_tail TEXT')
        # Migration v6 -> v7: worker-side verify (Sc1 #10) — the worker runs the
        # build/test gate in its own clone (parallel) and reports the result plus
        # the base sha it verified against, so the serial merge loop can skip the
        # re-verify when there is no rebase drift.
        if 'verified' not in cols:
            cur.execute('ALTER TABLE tasks ADD COLUMN verified INTEGER')
        if 'verified_base' not in cols:
            cur.execute('ALTER TABLE tasks ADD COLUMN verified_base TEXT')
        # Migration v7 -> v8: per-claim lease token. Identifies a specific claim
        # attempt so a stale /complete (or /heartbeat) from a prior attempt — even
        # by the SAME worker_id that later re-claimed the reaped task — cannot
        # mutate the current claim's state.
        if 'lease_token' not in cols:
            cur.execute('ALTER TABLE tasks ADD COLUMN lease_token TEXT')
        cur.execute(f'PRAGMA user_version = {SCHEMA_VERSION};')


# ── Capability tags ────────────────────────────────────────────────────────────

def parse_tags(value) -> set[str]:
    """Normalise a capability/requirement spec into a lowercase tag set.

    Accepts a list/tuple/set or a comma/whitespace-separated string. Tags are
    opaque strings by convention namespaced like 'os:windows', 'msbuild',
    'name:ar1'. Matching is case-insensitive.
    """
    if not value:
        return set()
    items = value if isinstance(value, (list, tuple, set)) else re.split(r'[\s,]+', value)
    return {t.strip().lower() for t in items if t and str(t).strip()}


def canon_tags(value) -> Optional[str]:
    """Canonical stored form: sorted, space-joined, or None when empty."""
    tags = parse_tags(value)
    return ' '.join(sorted(tags)) if tags else None


def ensure_lab_repo(lab_git: str = LAB_GIT, base_ref: str = DEFAULT_BASE_REF) -> None:
    """
    Guarantee lab.git exists as a bare repo whose base_ref already has at least
    one commit. An empty base_ref lets early workers branch off an unborn HEAD and
    create orphan roots that can never rebase — so we seed an empty root commit.
    Idempotent.
    """
    import shutil, tempfile
    if not os.path.isdir(lab_git):
        subprocess.run(['git', 'init', '--bare', '-b', base_ref, lab_git], check=True,
                       capture_output=True, text=True)
    # Smart-HTTP push via git-http-backend is refused with HTTP 403 unless the
    # bare repo explicitly enables receive-pack over HTTP. Set it idempotently so
    # a from-scratch host accepts worker pushes (prod lab.git had it set by hand).
    subprocess.run(['git', '--git-dir', lab_git, 'config', 'http.receivepack', 'true'],
                   check=True, capture_output=True, text=True)
    # base_ref already has commits? nothing to do.
    have = subprocess.run(['git', '--git-dir', lab_git, 'rev-parse', '--verify',
                           f'refs/heads/{base_ref}'], capture_output=True, text=True)
    if have.returncode == 0:
        return
    # Seed an empty root commit on base_ref via a throwaway worktree.
    wt = tempfile.mkdtemp(prefix='hive-seed-')
    try:
        env = {**os.environ,
               'GIT_AUTHOR_NAME': 'HAL', 'GIT_AUTHOR_EMAIL': 'hal@lab',
               'GIT_COMMITTER_NAME': 'HAL', 'GIT_COMMITTER_EMAIL': 'hal@lab'}
        subprocess.run(['git', 'clone', lab_git, wt], check=True,
                       capture_output=True, text=True)
        subprocess.run(['git', 'checkout', '-b', base_ref], cwd=wt,
                       capture_output=True, text=True)
        subprocess.run(['git', 'commit', '--allow-empty', '-m',
                        'hive: seed root commit'], cwd=wt, env=env, check=True,
                       capture_output=True, text=True)
        subprocess.run(['git', 'push', 'origin', base_ref], cwd=wt, check=True,
                       capture_output=True, text=True)
        log.info('seeded empty root commit on %s in %s', base_ref, lab_git)
    finally:
        shutil.rmtree(wt, ignore_errors=True)


# ── Task insertion (HAL control plane) ─────────────────────────────────────────

def add_task(
    task_id: str,
    prompt: str,
    branch: Optional[str] = None,
    base_ref: str = DEFAULT_BASE_REF,
    worker: Optional[str] = None,
    requires: Optional[list[str]] = None,
    scope: Optional[str] = None,
    priority: int = 5,
    complexity: Optional[int] = None,
    model: Optional[str] = None,
    deps: Optional[list[str]] = None,
    max_attempts: int = 3,
    db_path: str = QUEUE_DB,
) -> None:
    """Insert a task (+ its dependency edges) into the queue.

    `worker` pins the task to one exact lane. `requires` is a set of capability
    tags a worker must ALL advertise to be allowed to claim it (e.g.
    ['os:windows', 'msbuild']). Both are optional and both must pass.

    `complexity` (1..3) selects the model the worker runs copilot with; `model`
    is normally derived from it by the caller. If `model` is not given but
    `complexity` is, the model is filled in here from the shared mapping.
    """
    if model is None and complexity is not None:
        from hive_caps import model_for_complexity
        model = model_for_complexity(complexity)
    branch = branch or f'feature/{task_id}'
    with cursor(db_path) as cur:
        cur.execute(
            """INSERT INTO tasks
                 (id, branch, base_ref, prompt, worker, requires, scope,
                  priority, complexity, model, max_attempts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, branch, base_ref, prompt, worker, canon_tags(requires),
             scope, priority, complexity, model, max_attempts),
        )
        for dep in (deps or []):
            cur.execute(
                'INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES (?, ?)',
                (task_id, dep),
            )


# ── The claim (fixes C1 + C3) ──────────────────────────────────────────────────

@dataclass
class ClaimedTask:
    id: str
    branch: str
    base_ref: str
    prompt: str
    scope: Optional[str]
    attempts: int
    model: Optional[str] = None
    lease_token: Optional[str] = None


def claim_next(worker_id: str, worker_lane: Optional[str] = None,
               worker_caps: Optional[set[str]] = None,
               db_path: str = QUEUE_DB) -> Optional[ClaimedTask]:
    """Atomically claim the highest-priority claimable task, retrying on a
    transient SQLite lock (Sc3 #12). See _claim_next_once for the claim logic."""
    return with_retry(_claim_next_once_metered, worker_id, worker_lane, worker_caps, db_path)


def _claim_next_once_metered(worker_id, worker_lane, worker_caps, db_path):
    task = _claim_next_once(worker_id, worker_lane, worker_caps, db_path)
    if task is not None:
        hive_log.incr('claims')
        log.info('claimed %s by %s (lane=%s)', task.id, worker_id, worker_lane)
    return task


def _claim_next_once(worker_id: str, worker_lane: Optional[str] = None,
                     worker_caps: Optional[set[str]] = None,
                     db_path: str = QUEUE_DB) -> Optional[ClaimedTask]:
    """
    Atomically claim the highest-priority claimable task and return it, or None.

    Claimable = pending, past its not_before backoff gate, every dependency merged,
    (if worker_lane given) either unpinned or pinned to this lane, AND this worker's
    capabilities satisfy the task's `requires` set (requires ⊆ caps). Capability
    matching is done in Python over the priority-ordered candidates so subset logic
    stays simple; it remains atomic because BEGIN IMMEDIATE holds the write lock for
    the whole select-then-update.
    """
    caps = parse_tags(worker_caps) if worker_caps is not None else set()
    conn = connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE;')
        rows = conn.execute(
            """
            SELECT t.id, t.branch, t.base_ref, t.prompt, t.scope, t.attempts,
                   t.requires, t.model
            FROM tasks t
            WHERE t.status = 'pending'
              AND (t.not_before IS NULL OR t.not_before <= datetime('now'))
              AND (t.worker IS NULL OR t.worker = :lane)
              AND NOT EXISTS (
                    SELECT 1 FROM task_deps d
                    JOIN tasks dep ON dep.id = d.depends_on
                    WHERE d.task_id = t.id AND dep.status <> 'merged'
              )
            -- Sc5: order by DECLARED priority then FIFO. The old
            -- (priority - MIN(attempts,3)) term let a repeatedly-failing task's
            -- effective key drop below genuinely higher-priority fresh work, so
            -- a poison task climbed ahead and starved healthy work. Retries are
            -- already spaced by the not_before backoff gate above; within a
            -- priority, oldest-created wins (a retried task keeps its slot).
            ORDER BY t.priority ASC, t.created_at ASC
            """,
            {'lane': worker_lane},
        ).fetchall()

        # Pick the first candidate whose capability requirements this worker meets.
        chosen = next((r for r in rows if parse_tags(r['requires']) <= caps), None)
        if chosen is None:
            conn.execute('COMMIT;')
            return None

        lease_token = secrets.token_hex(8)
        conn.execute(
            """
            UPDATE tasks
            SET status        = 'claimed',
                worker_id     = :wid,
                lease_token   = :ltok,
                attempts      = attempts + 1,
                claimed_at    = datetime('now'),
                heartbeat_at  = datetime('now'),
                lease_expires = datetime('now', :lease)
            WHERE id = :tid
            """,
            {'wid': worker_id, 'ltok': lease_token, 'tid': chosen['id'],
             'lease': f'+{LEASE_MINUTES} minutes'},
        )
        conn.execute('COMMIT;')
        return ClaimedTask(
            id=chosen['id'], branch=chosen['branch'], base_ref=chosen['base_ref'],
            prompt=chosen['prompt'], scope=chosen['scope'],
            attempts=chosen['attempts'] + 1, model=chosen['model'],
            lease_token=lease_token,
        )
    except Exception:
        conn.execute('ROLLBACK;')
        raise
    finally:
        conn.close()


def mark_running(task_id: str, worker_id: str, db_path: str = QUEUE_DB) -> None:
    with cursor(db_path) as cur:
        cur.execute(
            """UPDATE tasks SET status='running', started_at=datetime('now')
               WHERE id=? AND worker_id=?""",
            (task_id, worker_id),
        )


# ── Heartbeat (fixes C3) ───────────────────────────────────────────────────────

def heartbeat(task_id: str, worker_id: str, db_path: str = QUEUE_DB,
              lease_token: Optional[str] = None) -> bool:
    """Renew the lease. Returns False if the task was reaped/reassigned (stop work).

    When `lease_token` is supplied it must match the token stored at claim time,
    so a worker that lost its claim (reaped, then re-claimed by anyone — including
    a new claim by the same worker_id) is told to abandon rather than renewing a
    lease it no longer owns."""
    with cursor(db_path) as cur:
        cur.execute(
            """
            UPDATE tasks
            SET heartbeat_at  = datetime('now'),
                lease_expires = datetime('now', ?)
            WHERE id = ? AND worker_id = ? AND status IN ('claimed','running')
              AND (? IS NULL OR lease_token = ?)
            """,
            (HEARTBEAT_RENEW, task_id, worker_id, lease_token, lease_token),
        )
        return cur.rowcount > 0


# ── Completion + retry/dead-letter (fixes M1) ─────────────────────────────────

def complete_task(
    task_id: str,
    worker_id: str,
    exit_code: int,
    commit_hash: Optional[str] = None,
    turns: Optional[int] = None,
    cost_pr: Optional[float] = None,
    log_path: Optional[str] = None,
    log_tail: Optional[str] = None,
    verified: Optional[bool] = None,
    verified_base: Optional[str] = None,
    error: Optional[str] = None,
    db_path: str = QUEUE_DB,
    lease_token: Optional[str] = None,
) -> str:
    """
    Record the outcome of a run. Success -> 'done' (merge loop takes it from here).
    Failure -> retry with exponential backoff, or dead-letter at max_attempts.
    Returns the resulting status, or 'stale' if the completion did not apply
    (the task was cancelled, reassigned, or this is a late completion from a
    superseded claim — see the ownership guards below).

    Every branch is guarded by `worker_id`, an active status, and (when supplied)
    the per-claim `lease_token`. This prevents a late/duplicate completion from a
    prior attempt — even one from the same worker_id that re-claimed the task —
    from clobbering the current claim, and prevents a completion from flipping a
    cancelled/merged task back into the pipeline.
    """
    hive_log.incr('completes')
    success = exit_code == 0 and bool(commit_hash)
    guard = "worker_id=? AND status IN ('claimed','running') AND (? IS NULL OR lease_token=?)"
    with cursor(db_path) as cur:
        row = cur.execute(
            'SELECT attempts, max_attempts FROM tasks WHERE id=?', (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f'unknown task {task_id}')

        if success:
            cur.execute(
                f"""UPDATE tasks
                   SET status='done', done_at=datetime('now'),
                       exit_code=?, commit_hash=?, turns=?, cost_pr=?, log_path=?,
                       log_tail=?, verified=?, verified_base=?,
                       lease_expires=NULL, heartbeat_at=NULL
                   WHERE id=? AND {guard}""",
                (exit_code, commit_hash, turns, cost_pr, log_path, log_tail,
                 (None if verified is None else int(bool(verified))), verified_base,
                 task_id, worker_id, lease_token, lease_token),
            )
            if cur.rowcount == 0:
                log.warning('ignored stale/late success for %s by %s', task_id, worker_id)
                return 'stale'
            return 'done'

        if row['attempts'] >= row['max_attempts']:
            hive_log.incr('dead_letters')
            log.warning('dead-lettered %s after %d attempts: %s',
                        task_id, row['attempts'], (error or '').strip()[:200])
            cur.execute(
                f"""UPDATE tasks
                   SET status='dead', done_at=datetime('now'),
                       exit_code=?, log_path=?, log_tail=?, error=?,
                       lease_expires=NULL, heartbeat_at=NULL
                   WHERE id=? AND {guard}""",
                (exit_code, log_path, log_tail, error,
                 task_id, worker_id, lease_token, lease_token),
            )
            return 'dead' if cur.rowcount else 'stale'

        hive_log.incr('retries')
        backoff = 2 ** row['attempts'] * 30      # 60s, 120s, 240s, ...
        log.info('retrying %s in %ds (attempt %d/%d): %s',
                 task_id, backoff, row['attempts'], row['max_attempts'],
                 (error or '').strip()[:200])
        cur.execute(
            f"""UPDATE tasks
               SET status='pending', worker_id=NULL,
                   not_before=datetime('now', ?),
                   lease_expires=NULL, heartbeat_at=NULL,
                   exit_code=?, log_path=?, log_tail=?, error=?
               WHERE id=? AND {guard}""",
            (f'+{backoff} seconds', exit_code, log_path, log_tail, error,
             task_id, worker_id, lease_token, lease_token),
        )
        return 'pending' if cur.rowcount else 'stale'


# ── Reaper (fixes C3) ─────────────────────────────────────────────────────────

def reap_once(db_path: str = QUEUE_DB) -> int:
    """Requeue tasks whose lease expired; dead-letter those out of attempts."""
    with cursor(db_path) as cur:
        cur.execute(
            f"""
            UPDATE tasks
            SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
                worker_id = NULL, lease_expires = NULL, heartbeat_at = NULL,
                error = COALESCE(error,'') ||
                        ' [reaped: lease expired @' || datetime('now') || ']'
            WHERE status IN {_INFLIGHT}
              AND lease_expires IS NOT NULL
              AND lease_expires < datetime('now')
            """
        )
        return cur.rowcount


def reaper_loop(stop: threading.Event, db_path: str = QUEUE_DB) -> None:
    while not stop.is_set():
        try:
            n = reap_once(db_path)
            if n:
                hive_log.incr('reaped', n)
                log.info('reaper requeued/dead-lettered %d orphaned task(s)', n)
        except Exception as e:  # never let the reaper die
            log.error('reaper error: %s', e)
        stop.wait(REAPER_INTERVAL_S)


# ── Merge loop (fixes C5 + M2) ────────────────────────────────────────────────

def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['git', '--git-dir', LAB_GIT, *args],
        capture_output=True, text=True, check=check,
    )


def _remove_worktree(wt: str) -> None:
    """Detach and delete a scratch worktree created by _rebase_worktree."""
    import shutil
    _git('worktree', 'remove', '--force', wt, check=False)
    shutil.rmtree(wt, ignore_errors=True)
    _git('worktree', 'prune', check=False)


def _rebase_worktree(branch: str, base_ref: str) -> Optional[str]:
    """
    Rebase `branch` onto `base_ref` inside a fresh scratch worktree of the bare
    repo and, on success, RETURN the worktree path so the caller can run the
    verify gate against the actual rebased tree (fixes the "verify runs in
    HIVE_DIR" gap). The caller owns cleanup via _remove_worktree.

    Returns None on rebase conflict (the worktree is cleaned up here). Bare repos
    have no worktree, so we attach a throwaway one; the checked-out branch ref
    follows the rebase.
    """
    import tempfile
    # Rebase creates new commits, so git needs a committer identity even in a
    # scratch worktree (else it aborts with "Committer identity unknown", which
    # must NOT be misread as a merge conflict).
    env = {**os.environ,
           'GIT_AUTHOR_NAME': 'HAL', 'GIT_AUTHOR_EMAIL': 'hal@lab',
           'GIT_COMMITTER_NAME': 'HAL', 'GIT_COMMITTER_EMAIL': 'hal@lab'}
    wt = tempfile.mkdtemp(prefix='hive-rebase-')
    add = _git('worktree', 'add', '-f', '--checkout', wt, branch, check=False)
    if add.returncode != 0:
        _remove_worktree(wt)
        return None
    rb = subprocess.run(['git', 'rebase', base_ref], cwd=wt, env=env,
                        capture_output=True, text=True)
    if rb.returncode != 0:
        subprocess.run(['git', 'rebase', '--abort'], cwd=wt, env=env,
                       capture_output=True, text=True)
        _remove_worktree(wt)
        return None
    return wt


def _deps_all_merged(cur: sqlite3.Cursor, task_id: str) -> bool:
    row = cur.execute(
        """SELECT COUNT(*) AS n FROM task_deps d
           JOIN tasks dep ON dep.id = d.depends_on
           WHERE d.task_id = ? AND dep.status <> 'merged'""",
        (task_id,),
    ).fetchone()
    return row['n'] == 0


def _park(cur: sqlite3.Cursor, task_id: str, reason: str) -> None:
    cur.execute(
        """UPDATE tasks SET status='needs_attention', error=?,
                            lease_expires=NULL WHERE id=?""",
        (reason, task_id),
    )
    hive_log.incr('parks')
    log.warning('parked %s: %s', task_id, reason)


def _set_status(db_path: str, task_id: str, status: str,
                commit_hash: Optional[str] = None,
                lease: Optional[str] = None,
                clear_lease: bool = False) -> None:
    """Short-lived write for a single merge-loop state transition. Keeping each
    flip in its own tiny cursor block means the (slow) git rebase + verify
    subprocesses in _merge_one run with NO DB cursor open, so they can never
    stall other writers regardless of the connection's isolation level.

    `lease` (a sqlite datetime modifier, e.g. '+30 minutes') stamps
    lease_expires so a crash mid-transition is recoverable; `clear_lease` wipes
    it on reaching a terminal state."""
    sets = ['status=?']
    params: list = [status]
    if commit_hash is not None:
        sets.append('commit_hash=?')
        params.append(commit_hash)
    if lease is not None:
        sets.append("lease_expires=datetime('now', ?)")
        params.append(lease)
    elif clear_lease:
        sets.append('lease_expires=NULL')
    params.append(task_id)
    with cursor(db_path) as cur:
        cur.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", params)


def _park_task(db_path: str, task_id: str, reason: str) -> None:
    with cursor(db_path) as cur:
        _park(cur, task_id, reason)


def verify_branch(branch: str, base_ref: str, scope: Optional[str],
                  cwd: Optional[str] = None) -> tuple[bool, str]:
    """
    Build/test gate (§7). Runs HIVE_VERIFY_CMD (a shell command) against a
    checkout of the rebased branch and returns (ok, summary).

    `cwd` MUST be a worktree containing the rebased branch tip (the merge loop
    passes the worktree from _rebase_worktree). Running the command there is what
    makes the gate real — otherwise it would exercise unrelated content in the
    hive tooling directory and prove nothing about the task. If `cwd` is None it
    falls back to HIVE_DIR (kept only for ad-hoc/manual calls).

    The command runs locally on the HAL host. Building code that requires a
    different toolchain/OS (e.g. a Windows .NET build) is out of scope here and
    tracked separately; there is no remote-host execution.

    FAIL-CLOSED by default: if HIVE_VERIFY_CMD is unset the gate does NOT pass —
    the task is parked `needs_attention` so unverified code can never fast-forward
    into the base ref silently. To intentionally run without a build/test gate
    (e.g. a throwaway lab), set HIVE_VERIFY_OPTIONAL=1 to opt out explicitly.
    """
    cmd = os.environ.get('HIVE_VERIFY_CMD')
    if not cmd:
        if os.environ.get('HIVE_VERIFY_OPTIONAL', '').strip().lower() in ('1', 'true', 'yes', 'on'):
            return True, 'verify skipped (HIVE_VERIFY_OPTIONAL set; no HIVE_VERIFY_CMD)'
        return False, ('verify gate fail-closed: set HIVE_VERIFY_CMD to a build/test '
                       'command, or HIVE_VERIFY_OPTIONAL=1 to opt out explicitly')
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          cwd=cwd or HIVE_DIR)
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-5:]
    return proc.returncode == 0, ' | '.join(tail)


def _merge_one(db_path: str, task: sqlite3.Row) -> None:
    """Rebase → verify → fast-forward one 'done' task into its base_ref.

    The git rebase and the verify subprocess can be slow (a real build/test).
    They run here with NO DB cursor open; only the short state transitions
    (verifying / merged / needs_attention) touch the database, each in its own
    _set_status / _park_task write. The merge loop stays single-threaded
    (serialized merges) but never holds a lock across the slow work.
    """
    branch, base_ref = task['branch'], task['base_ref'] or DEFAULT_BASE_REF
    task_id = task['id']
    # Stamp a lease on 'verifying' so a merge-loop crash mid-transition is
    # recoverable: merge_once sweeps expired 'verifying' rows back to 'done'
    # (see _sweep_stale_verifying). Without this the row would have a NULL lease,
    # the reaper (in-flight only) would skip it, and it would hang forever.
    _set_status(db_path, task_id, 'verifying', lease=VERIFY_LEASE)

    # base_ref is guaranteed to exist (ensure_lab_repo seeds a root commit), so we
    # always rebase then fast-forward — no orphan-seed special case.
    if _git('rev-parse', '--verify', f'refs/heads/{base_ref}',
            check=False).returncode != 0:
        _park_task(db_path, task_id, f'base_ref {base_ref} missing (run ensure_lab_repo)')
        return

    # M2: rebase branch onto the LIVE base so we never merge stale. lab.git is
    # bare (no worktree), so rebase runs in a scratch worktree; _rebase_worktree
    # RETURNS that worktree so the verify gate can run against the rebased tip.
    # Capture the base tip we rebase onto so the final ref move can be a
    # compare-and-swap (see the update-ref below) — never a blind force-move.
    base_old = _git('rev-parse', f'refs/heads/{base_ref}', check=False).stdout.strip()
    wt = _rebase_worktree(branch, base_ref)
    if wt is None:
        _park_task(db_path, task_id, f'rebase conflict onto {base_ref}')
        return

    try:
        # Sc1 (#10): the worker may have already run the build/test gate in its
        # own clone (in parallel with every other worker). We trust that result
        # ONLY when the tree we are about to fast-forward is byte-identical to the
        # tree the worker actually tested — i.e. rebasing onto the live base
        # produced exactly the worker's verified commit tree. That is a stronger,
        # provably-sound guard than "the base sha didn't move": it also holds up
        # if the agent produced merge commits or non-linear history that a rebase
        # would otherwise reshape. Any mismatch (base drifted, rebase reshaped the
        # tree, or the worker reported no verify) falls back to the server gate,
        # preserving fail-closed safety — no unverified tree can land.
        worker_verified = task['verified']          # None=not run, 0=failed, 1=passed
        worker_commit = task['commit_hash']
        worker_tree = _git('rev-parse', f'{worker_commit}^{{tree}}',
                           check=False).stdout.strip() if worker_commit else ''
        rebased_tree = subprocess.run(
            ['git', 'rev-parse', 'HEAD^{tree}'], cwd=wt,
            capture_output=True, text=True).stdout.strip()
        trees_match = bool(worker_tree) and worker_tree == rebased_tree
        trust_worker = worker_verified is not None and trees_match
        if trust_worker:
            ok = bool(worker_verified)
            summary = ('verified on worker (rebased tree identical)' if ok
                       else 'verify failed on worker (rebased tree identical)')
            log.info('merge %s: trusting worker verify (%s), base=%s',
                     task_id, 'pass' if ok else 'fail', task['verified_base'])
        else:
            # C5: build/test gate BEFORE merge — no unverified code lands. The gate
            # runs in `wt` (the rebased checkout), so it exercises the task's actual
            # code, not the hive tooling dir.
            ok, summary = verify_branch(branch, base_ref, task['scope'], cwd=wt)
        if not ok:
            _park_task(db_path, task_id, f'verify failed: {summary}')
            return

        # Advance base_ref to the rebased branch tip. Use update-ref as a
        # compare-and-swap against the base tip we rebased onto (base_old): the
        # rebased branch is by construction a descendant of base_old, so this is
        # a true fast-forward. If base_ref moved since (it should not — the merge
        # loop is single-threaded — but an external push or a future second
        # merger would), the CAS fails and we park rather than force-moving the
        # ref and silently dropping the commits that advanced it. Never `branch
        # -f`, which would rewind base on any drift.
        tip = _git('rev-parse', branch, check=False).stdout.strip() or None
        res = _git('update-ref', f'refs/heads/{base_ref}', tip, base_old, check=False)
        if res.returncode != 0:
            _park_task(db_path, task_id,
                       f'merge into {base_ref} failed (base moved under merge; '
                       f'expected {base_old[:8]}): {res.stderr.strip()}')
            return
        _set_status(db_path, task_id, 'merged', commit_hash=tip, clear_lease=True)
        hive_log.incr('merges')
        log.info('merged %s → %s (%s)', branch, base_ref, task_id)
    finally:
        _remove_worktree(wt)


def _sweep_stale_verifying(db_path: str) -> int:
    """Recover tasks orphaned in 'verifying' by a merge-loop crash/restart.

    A 'verifying' row carries a lease (VERIFY_LEASE). If the single merge thread
    dies before reaching a terminal state, the row is stuck: the reaper only
    reclaims in-flight (claimed/running) leases, and the merge selector only
    picks status='done'. Once the lease expires we reset it to 'done' so the
    next merge pass re-processes it (rebase+verify are idempotent)."""
    with cursor(db_path) as cur:
        cur.execute(
            """UPDATE tasks SET status='done', lease_expires=NULL
               WHERE status='verifying'
                 AND lease_expires IS NOT NULL
                 AND lease_expires < datetime('now')"""
        )
        n = cur.rowcount
    if n:
        log.info('merge loop recovered %d stale verifying task(s) -> done', n)
    return n


def merge_once(db_path: str = QUEUE_DB) -> int:
    """Process all mergeable 'done' tasks, oldest-first, serialized. Returns count.

    Snapshots the mergeable set under a short read cursor, then closes it before
    doing any (slow) git/verify work in _merge_one — so the rebase/verify
    subprocesses never run with a DB cursor held open.
    """
    _sweep_stale_verifying(db_path)  # recover crash-orphaned 'verifying' first
    with cursor(db_path) as cur:
        rows = cur.execute(
            """SELECT * FROM tasks WHERE status='done'
               ORDER BY done_at ASC"""
        ).fetchall()
        mergeable = [t for t in rows if _deps_all_merged(cur, t['id'])]

    merged = 0
    for task in mergeable:
        _merge_one(db_path, task)
        merged += 1
    return merged


def merge_loop(stop: threading.Event, db_path: str = QUEUE_DB) -> None:
    while not stop.is_set():
        try:
            merge_once(db_path)
        except Exception as e:  # never let the merge loop die
            log.error('merge loop error: %s', e)
        stop.wait(MERGE_INTERVAL_S)


# ── Status / control helpers ──────────────────────────────────────────────────

def _task_title(prompt: str, limit: int = 72) -> str:
    """Derive a short human label from a task prompt for at-a-glance dashboards.

    The prompt may be a large embedded blob (e.g. a pre-fetched PR diff for
    review). We want the intent, not the payload, so: take the first non-empty
    line, strip a leading markdown heading / list marker, and cap the length.
    Recognises the ADO-review prompt convention (a leading 'Review ... PR <n>'
    or a '# PR <n>' heading) so those read nicely too."""
    if not prompt:
        return ''
    line = ''
    for raw in prompt.splitlines():
        s = raw.strip()
        if s:
            line = s
            break
    line = line.lstrip('#>-*').strip()
    if len(line) > limit:
        line = line[:limit - 1].rstrip() + '…'
    return line


def upsert_worker(worker_id: str, lane: Optional[str] = None,
                  caps=None, status: str = 'idle',
                  current_task: Optional[str] = None,
                  host: Optional[str] = None,
                  db_path: str = QUEUE_DB) -> None:
    """Register/refresh a worker's liveness row. Called by the server on every
    /claim (idle heartbeat via the 25s long-poll) and /heartbeat (busy, 60s), so
    an idle worker stays fresh without any extra client code. lane/caps/host are
    preserved (COALESCE) when a later call omits them."""
    if isinstance(caps, (list, tuple, set)):
        caps = ' '.join(sorted(str(c) for c in caps))
    with cursor(db_path) as cur:
        cur.execute(
            """
            INSERT INTO workers
                (worker_id, lane, caps, host, status, current_task,
                 first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(worker_id) DO UPDATE SET
                lane         = COALESCE(excluded.lane, workers.lane),
                caps         = COALESCE(excluded.caps, workers.caps),
                host         = COALESCE(excluded.host, workers.host),
                status       = excluded.status,
                current_task = excluded.current_task,
                last_seen    = datetime('now')
            """,
            (worker_id, lane, caps, host, status, current_task),
        )


def list_workers(stale_after_s: int = WORKER_STALE_S,
                 db_path: str = QUEUE_DB) -> list:
    """Return every registered worker with a derived `online` flag and `age_s`
    (seconds since last_seen). A worker past stale_after_s is treated offline."""
    with cursor(db_path) as cur:
        rows = cur.execute(
            """SELECT worker_id, lane, caps, host, status, current_task,
                      first_seen, last_seen,
                      CAST((julianday('now') - julianday(last_seen)) * 86400
                           AS INTEGER) AS age_s
               FROM workers ORDER BY worker_id"""
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        age = d.get('age_s')
        d['online'] = age is not None and age <= stale_after_s
        # A stale row can't really be "busy" — reflect reality for the UI.
        if not d['online'] and d['status'] == 'busy':
            d['status'] = 'stale'
        out.append(d)
    return out


def status_snapshot(db_path: str = QUEUE_DB) -> dict:
    with cursor(db_path) as cur:
        counts = {
            r['status']: r['n']
            for r in cur.execute(
                'SELECT status, COUNT(*) AS n FROM tasks GROUP BY status'
            ).fetchall()
        }
        rows = [dict(r) for r in cur.execute(
            """SELECT id, status, worker_id, worker, requires, priority,
                      complexity, model, branch, commit_hash, error, prompt,
                      created_at, done_at
               FROM tasks ORDER BY
                 CASE status WHEN 'running' THEN 0 WHEN 'claimed' THEN 1
                             WHEN 'pending' THEN 2 ELSE 3 END,
                 priority ASC, created_at ASC"""
        ).fetchall()]
    for row in rows:
        # Replace the (potentially huge) prompt with a short human title so the
        # dashboard shows intent, not the full payload, and stays lightweight.
        row['title'] = _task_title(row.pop('prompt', ''))
    return {'counts': counts, 'tasks': rows, 'workers': list_workers(db_path=db_path)}


def get_log_tail(task_id: str, db_path: str = QUEUE_DB) -> Optional[str]:
    """Return the stored tail of the worker copilot log for a task, or None if
    the task is unknown or produced no log (U2 #16). This is what makes failure
    triage possible from HAL without SSHing the worker."""
    with cursor(db_path) as cur:
        row = cur.execute(
            'SELECT log_tail FROM tasks WHERE id=?', (task_id,)
        ).fetchone()
    if row is None:
        return None
    return row['log_tail']


def cancel_task(task_id: str, db_path: str = QUEUE_DB) -> bool:
    """Cancel a task in any pre-merge state."""
    with cursor(db_path) as cur:
        cur.execute(
            """UPDATE tasks SET status='cancelled', worker_id=NULL, lease_token=NULL,
                   lease_expires=NULL, heartbeat_at=NULL
               WHERE id=? AND status NOT IN ('merged','cancelled','dead')""",
            (task_id,),
        )
        return cur.rowcount > 0


def reprioritize(task_id: str, priority: int, db_path: str = QUEUE_DB) -> bool:
    with cursor(db_path) as cur:
        cur.execute('UPDATE tasks SET priority=? WHERE id=?', (priority, task_id))
        return cur.rowcount > 0


def requeue_task(task_id: str, reset_attempts: bool = True,
                 db_path: str = QUEUE_DB) -> bool:
    """Re-enqueue a parked task (needs_attention | dead | cancelled) back to
    'pending' so a worker can pick it up again. Clears the error, worker binding,
    lease/heartbeat and the not_before backoff gate; by default also resets the
    attempt counter so a fresh run gets its full max_attempts budget (U1).

    Returns False if the task doesn't exist or isn't in a re-queueable state
    (e.g. already pending/running/merged)."""
    attempts_sql = ', attempts=0' if reset_attempts else ''
    with cursor(db_path) as cur:
        cur.execute(
            f"""UPDATE tasks
                SET status='pending', error=NULL, worker_id=NULL,
                    lease_expires=NULL, heartbeat_at=NULL, not_before=NULL
                    {attempts_sql}
                WHERE id=? AND status IN ('needs_attention','dead','cancelled')""",
            (task_id,),
        )
        return cur.rowcount > 0


if __name__ == '__main__':
    init_db()
    print(f'queue.db initialised at {QUEUE_DB} (schema v{SCHEMA_VERSION})')
