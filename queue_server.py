#!/usr/bin/env python3
"""
HAL — Queue REST server (Phase 9.1 §6)

The ONLY interface Windows workers use to reach queue.db. Runs on HAL, binds the
LAN. Windows workers never touch the SQLite file directly (locking is broken over
SMB/NFS), so this server is also the single writer boundary for remote lanes.

Endpoints:
    POST /claim      {worker_id, lane?}          -> 200 task | 204 empty
    POST /heartbeat  {task_id, worker_id}        -> 200 {alive: bool}
    POST /complete   {task_id, worker_id, ...}   -> 200 {status}
    POST /add        {id, prompt, ...}           -> 201
    POST /cancel     {task_id}                   -> 200 {cancelled: bool}
    GET  /status                                 -> 200 fleet snapshot

Auth: static bearer token (Authorization: Bearer <token>). The LAN is not trusted.
Token is read from $HIVE_TOKEN or ~/.hive/worker.token.

/claim long-polls up to 25s (with jitter) waiting for work, killing SSH-per-poll
handshake storms and thundering-herd wakeups (M4).

Also starts the reaper + merge-loop background threads so a single process runs
the whole control plane.
"""

from __future__ import annotations

import os
import random
import threading
import time

from flask import Flask, jsonify, request

import hive_queue as q

# ── Auth ──────────────────────────────────────────────────────────────────────

TOKEN_FILE = os.path.expanduser('~/.hive/worker.token')


def _load_token() -> str:
    tok = os.environ.get('HIVE_TOKEN')
    if tok:
        return tok.strip()
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    # First run: generate and persist a token.
    tok = os.urandom(24).hex()
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, 'w') as f:
        f.write(tok + '\n')
    os.chmod(TOKEN_FILE, 0o600)
    print(f'[server] generated worker token -> {TOKEN_FILE}')
    return tok


TOKEN = _load_token()

# ── Long-poll tuning (§6) ─────────────────────────────────────────────────────

CLAIM_LONGPOLL_S = 25       # max server-side wait for /claim
CLAIM_POLL_STEP  = 1.0      # DB re-check cadence within a long-poll

app = Flask(__name__)


def _authorized() -> bool:
    hdr = request.headers.get('Authorization', '')
    return hdr.startswith('Bearer ') and hdr[7:].strip() == TOKEN


@app.before_request
def _guard():
    if request.method == 'GET' and request.path == '/health':
        return None
    if not _authorized():
        return jsonify(error='unauthorized'), 401


@app.get('/health')
def health():
    return jsonify(ok=True)


@app.post('/claim')
def claim():
    body = request.get_json(force=True, silent=True) or {}
    worker_id = body.get('worker_id')
    lane = body.get('lane')
    caps = body.get('caps')
    if not worker_id:
        return jsonify(error='worker_id required'), 400

    deadline = time.time() + CLAIM_LONGPOLL_S
    while True:
        task = q.claim_next(worker_id, lane, worker_caps=caps)
        if task is not None:
            return jsonify(
                id=task.id, branch=task.branch, base_ref=task.base_ref,
                prompt=task.prompt, scope=task.scope, attempts=task.attempts,
            ), 200
        if time.time() >= deadline:
            return '', 204
        time.sleep(CLAIM_POLL_STEP + random.uniform(0, 0.5))   # jitter vs herd


@app.post('/heartbeat')
def heartbeat():
    body = request.get_json(force=True, silent=True) or {}
    alive = q.heartbeat(body['task_id'], body['worker_id'])
    return jsonify(alive=alive), 200


@app.post('/complete')
def complete():
    b = request.get_json(force=True, silent=True) or {}
    status = q.complete_task(
        b['task_id'], b['worker_id'], int(b.get('exit_code', 1)),
        commit_hash=b.get('commit_hash'), turns=b.get('turns'),
        cost_pr=b.get('cost_pr'), log_path=b.get('log_path'), error=b.get('error'),
    )
    return jsonify(status=status), 200


@app.post('/add')
def add():
    b = request.get_json(force=True, silent=True) or {}
    q.add_task(
        b['id'], b['prompt'], branch=b.get('branch'),
        base_ref=b.get('base_ref', q.DEFAULT_BASE_REF), worker=b.get('worker'),
        requires=b.get('requires'), scope=b.get('scope'),
        priority=int(b.get('priority', 5)),
        deps=b.get('deps'), max_attempts=int(b.get('max_attempts', 3)),
    )
    return jsonify(id=b['id']), 201


@app.post('/cancel')
def cancel():
    b = request.get_json(force=True, silent=True) or {}
    return jsonify(cancelled=q.cancel_task(b['task_id'])), 200


@app.get('/status')
def status():
    return jsonify(q.status_snapshot()), 200


def main():
    q.init_db()
    q.ensure_lab_repo()
    stop = threading.Event()
    threading.Thread(target=q.reaper_loop, args=(stop,), daemon=True).start()
    threading.Thread(target=q.merge_loop, args=(stop,), daemon=True).start()
    host = os.environ.get('HIVE_BIND', '0.0.0.0')
    port = int(os.environ.get('HIVE_PORT', '5001'))
    print(f'[server] queue REST on {host}:{port} — reaper + merge-loop running')
    print(f'[server] token: Authorization: Bearer {TOKEN}')
    # threaded=True so long-poll /claim calls don't block the reaper/other workers.
    app.run(host=host, port=port, threaded=True)


if __name__ == '__main__':
    main()
