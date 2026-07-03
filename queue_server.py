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

from flask import Flask, Response, jsonify, request

import hive_queue as q
import bootstrap as bootstrap

DASHBOARD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'dashboard.html')
HIVE_DIR = os.path.dirname(os.path.abspath(__file__))

# Files a fresh worker may pull over /download during bootstrap. Allowlisted so
# the endpoint can never serve queue.db, tokens, or arbitrary paths.
DOWNLOADABLE = {
    'worker_loop.py': 'text/x-python',
    'hive_caps.py':   'text/x-python',
    'hive_queue.py':  'text/x-python',
    'winsw.exe':      'application/octet-stream',
}

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

RO_TOKEN_FILE = os.path.expanduser('~/.hive/readonly.token')


def _load_ro_token() -> str:
    """Low-privilege READ-ONLY token for the dashboard.

    Distinct from the control TOKEN so the no-auth dashboard HTML can carry a
    token that only grants GET /status — never add/cancel/claim. Resolved from
    $HIVE_RO_TOKEN or ~/.hive/readonly.token (auto-generated on first run).
    """
    tok = os.environ.get('HIVE_RO_TOKEN')
    if tok:
        return tok.strip()
    if os.path.exists(RO_TOKEN_FILE):
        with open(RO_TOKEN_FILE) as f:
            return f.read().strip()
    tok = os.urandom(24).hex()
    os.makedirs(os.path.dirname(RO_TOKEN_FILE), exist_ok=True)
    with open(RO_TOKEN_FILE, 'w') as f:
        f.write(tok + '\n')
    os.chmod(RO_TOKEN_FILE, 0o600)
    print(f'[server] generated read-only token -> {RO_TOKEN_FILE}')
    return tok


RO_TOKEN = _load_ro_token()

# GET endpoints a read-only token may reach (the control token also works).
READONLY_PATHS = frozenset({'/status'})

# ── Long-poll tuning (§6) ─────────────────────────────────────────────────────

CLAIM_LONGPOLL_S = 25       # max server-side wait for /claim
CLAIM_POLL_STEP  = 1.0      # DB re-check cadence within a long-poll

app = Flask(__name__)


def _authorized() -> bool:
    """True for the full-privilege control token."""
    hdr = request.headers.get('Authorization', '')
    return hdr.startswith('Bearer ') and hdr[7:].strip() == TOKEN


def _read_authorized() -> bool:
    """True for EITHER the control token or the read-only token (GET /status)."""
    hdr = request.headers.get('Authorization', '')
    if not hdr.startswith('Bearer '):
        return False
    tok = hdr[7:].strip()
    return tok == TOKEN or tok == RO_TOKEN


@app.before_request
def _guard():
    # Truly public, secret-free GET endpoints (no token, no secrets served).
    if request.method == 'GET' and request.path in ('/health', '/', '/dashboard'):
        return None
    # Read-only endpoints accept the low-privilege dashboard token as well.
    if request.method == 'GET' and request.path in READONLY_PATHS:
        if _read_authorized():
            return None
        return jsonify(error='unauthorized'), 401
    # Everything else needs the control token — INCLUDING /bootstrap.* and
    # /download/*, which serve the control + GH OAuth tokens (or the worker code
    # that uses them). Previously these were unauthenticated, so anyone who could
    # reach :5001 could pull both secrets. (S1 #4)
    if not _authorized():
        return jsonify(error='unauthorized'), 401


@app.get('/health')
def health():
    return jsonify(ok=True)


@app.get('/download/<path:name>')
def download(name):
    """Serve allowlisted worker assets to a bootstrapping node.

    Requires the control token (enforced by _guard) — the bootstrap script
    authenticates these fetches with the token it was handed. Allowlisted so the
    endpoint can never serve queue.db, tokens, or arbitrary paths."""
    mime = DOWNLOADABLE.get(name)
    if mime is None:
        return jsonify(error='not found'), 404
    path = os.path.join(HIVE_DIR, name)
    if not os.path.isfile(path):
        return jsonify(error='missing on server'), 404
    with open(path, 'rb') as f:
        return Response(f.read(), mimetype=mime)


@app.get('/bootstrap.ps1')
@app.get('/bootstrap.sh')
def bootstrap_script():
    """Return a ready-to-run worker setup script for a fresh box.

    Query: lane (required), caps (comma/space list, optional), id (worker-id,
    default '<lane>#1'). `.ps1` -> Windows/WinSW, `.sh` -> Linux(systemd)/
    macOS(launchd). The queue + copilot tokens are injected server-side, so the
    operator just runs the one-liner shown on the dashboard.

    Requires the control token (enforced by _guard): the returned script embeds
    the control + GH OAuth tokens, so joining a worker is a privileged action.
    The dashboard builds the one-liner with the operator-supplied control token
    (kept only in the browser), never rendering it into the page. (S1 #4)"""
    os_kind = 'windows' if request.path.endswith('.ps1') else 'unix'
    lane = (request.args.get('lane') or '').strip()
    if not lane:
        return Response('# error: ?lane= is required\n', status=400,
                        mimetype='text/plain')
    caps = request.args.get('caps', '')
    wid = request.args.get('id', '')
    body = bootstrap.build_script(os_kind, lane, caps, wid, TOKEN)
    return Response(body, mimetype='text/plain')


@app.get('/')
@app.get('/dashboard')
def dashboard():
    """Serve the single-page control-plane dashboard.

    The page is unauthenticated (host-only LAN trust). It embeds only the
    low-privilege READ-ONLY token so its /status polling works; that token
    cannot add/cancel/claim. The full control token is NEVER rendered into the
    HTML — for control actions (cancel) the operator pastes the control token
    into the page, and it lives only in their browser's sessionStorage.
    """
    try:
        with open(DASHBOARD_FILE, encoding='utf-8') as f:
            html = f.read()
    except OSError as exc:
        return Response(f'dashboard unavailable: {exc}', status=500,
                        mimetype='text/plain')
    html = html.replace('%RO_TOKEN%', RO_TOKEN)
    return Response(html, mimetype='text/html')


@app.post('/claim')
def claim():
    body = request.get_json(force=True, silent=True) or {}
    worker_id = body.get('worker_id')
    lane = body.get('lane')
    caps = body.get('caps')
    if not worker_id:
        return jsonify(error='worker_id required'), 400

    host = request.remote_addr
    # Register/refresh as idle up-front so an idle worker (which spends its life
    # long-polling here) shows on the dashboard even before it claims anything.
    q.upsert_worker(worker_id, lane=lane, caps=caps, status='idle',
                    current_task=None, host=host)

    deadline = time.time() + CLAIM_LONGPOLL_S
    while True:
        task = q.claim_next(worker_id, lane, worker_caps=caps)
        if task is not None:
            q.upsert_worker(worker_id, lane=lane, caps=caps, status='busy',
                            current_task=task.id, host=host)
            return jsonify(
                id=task.id, branch=task.branch, base_ref=task.base_ref,
                prompt=task.prompt, scope=task.scope, attempts=task.attempts,
                model=task.model,
            ), 200
        if time.time() >= deadline:
            return '', 204
        time.sleep(CLAIM_POLL_STEP + random.uniform(0, 0.5))   # jitter vs herd


@app.post('/heartbeat')
def heartbeat():
    body = request.get_json(force=True, silent=True) or {}
    alive = q.heartbeat(body['task_id'], body['worker_id'])
    # A heartbeat means the worker is busy on this task — keep its liveness fresh.
    q.upsert_worker(body['worker_id'], status='busy',
                    current_task=body['task_id'], host=request.remote_addr)
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
        complexity=(int(b['complexity']) if b.get('complexity') is not None else None),
        model=b.get('model'),
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
    print(f'[server] control token: Authorization: Bearer <HIVE_TOKEN>  (add/cancel/claim)')
    print(f'[server] read-only token (dashboard /status) -> {RO_TOKEN_FILE}')
    # threaded=True so long-poll /claim calls don't block the reaper/other workers.
    app.run(host=host, port=port, threaded=True)


if __name__ == '__main__':
    main()
