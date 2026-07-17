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
import json
import hashlib
import random
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import hmac

from flask import Flask, Response, jsonify, request

import hive_queue as q
import hive_log
import bootstrap as bootstrap
from hive_version import CODE_VERSION

log = hive_log.get_logger('hive.server')

DASHBOARD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'dashboard.html')
HIVE_DIR = os.path.dirname(os.path.abspath(__file__))

# Files a fresh worker may pull over /download during bootstrap. Allowlisted so
# the endpoint can never serve queue.db, tokens, or arbitrary paths.
DOWNLOADABLE = {
    'worker_loop.py': 'text/x-python',
    'hive_caps.py':   'text/x-python',
    'hive_queue.py':  'text/x-python',
    'hive_log.py':    'text/x-python',
    'hive_version.py': 'text/x-python',
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
    log.info('generated worker token -> %s', TOKEN_FILE)
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
    log.info('generated read-only token -> %s', RO_TOKEN_FILE)
    return tok


RO_TOKEN = _load_ro_token()


def join_host():
    """Canonical address a REMOTE worker should dial to reach this queue.

    Never loopback: the join one-liner runs on another box, where 127.0.0.1 /
    localhost mean "that box itself". Prefer HIVE_ADVERTISE, else the first
    non-loopback bind address, else the lab IP (HIVE_LAB_IP)."""
    adv = os.environ.get('HIVE_ADVERTISE')
    if adv and adv.strip():
        return adv.strip()
    for a in resolve_bind_addrs(os.environ.get('HIVE_BIND')):
        if a not in ('127.0.0.1', '0.0.0.0', '::', '::1', 'localhost'):
            return a
    return os.environ.get('HIVE_LAB_IP', '192.168.100.1').strip()


def join_origin():
    """scheme://host:port a fresh worker should bootstrap from (see join_host)."""
    scheme = 'https' if (os.environ.get('HIVE_TLS_CERT')
                         and os.environ.get('HIVE_TLS_KEY')) else 'http'
    port = os.environ.get('HIVE_PORT', '5001').strip()
    return f'{scheme}://{join_host()}:{port}'


# GET endpoints a read-only token may reach (the control token also works).
# /logs/<task> is matched by prefix in _guard (it carries a path parameter).
READONLY_PATHS = frozenset({'/status', '/metrics'})

# ── Long-poll tuning (§6) ─────────────────────────────────────────────────────

CLAIM_LONGPOLL_S = 25       # max server-side wait for /claim
CLAIM_POLL_STEP  = 1.0      # DB re-check cadence within a long-poll

app = Flask(__name__)


def _authorized() -> bool:
    """True for the full-privilege control token. Constant-time compare (S4)."""
    hdr = request.headers.get('Authorization', '')
    if not hdr.startswith('Bearer '):
        return False
    return hmac.compare_digest(hdr[7:].strip(), TOKEN)


def _read_authorized() -> bool:
    """True for EITHER the control token or the read-only token (GET /status).

    Uses hmac.compare_digest and evaluates both comparisons (no short-circuit)
    so auth never leaks token bytes via response timing. (S4)"""
    hdr = request.headers.get('Authorization', '')
    if not hdr.startswith('Bearer '):
        return False
    tok = hdr[7:].strip()
    is_ctl = hmac.compare_digest(tok, TOKEN)
    is_ro = hmac.compare_digest(tok, RO_TOKEN)
    return is_ctl or is_ro


@app.before_request
def _guard():
    # Truly public, secret-free GET endpoints (no token, no secrets served).
    if request.method == 'GET' and request.path in ('/health', '/', '/dashboard'):
        return None
    # Read-only endpoints accept the low-privilege dashboard token as well.
    # /logs/<task> is read-only observability, matched by prefix (path param).
    if request.method == 'GET' and (request.path in READONLY_PATHS
                                    or request.path.startswith('/logs/')
                                    or request.path.startswith('/task/')):
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


def _code_manifest() -> dict:
    """sha256 of every downloadable .py asset present on disk. Workers verify
    each fetched file against this before swapping it in (integrity gate)."""
    files = {}
    for name in DOWNLOADABLE:
        if not name.endswith('.py'):
            continue
        path = os.path.join(HIVE_DIR, name)
        if os.path.isfile(path):
            with open(path, 'rb') as f:
                files[name] = hashlib.sha256(f.read()).hexdigest()
    return files


@app.get('/version')
def version():
    """Advertise the current worker code version + per-file integrity manifest.
    Control-token gated (a worker fetches it with the same token it claims with).
    The version number comes from hive_version.CODE_VERSION; bump that when you
    change a downloadable asset and want the fleet to self-update."""
    return jsonify(version=CODE_VERSION, files=_code_manifest())


# ── Central secret distribution (control-token gated) ─────────────────────────
# Workers need credentials for private feeds (e.g. the 1id Azure Artifacts NuGet
# feeds) to build ARS. Rather than rot a static PAT into every worker profile
# (fragile: per-user, WOW64-redirected, and the cached tokens EXPIRE), the server
# mints a fresh Azure DevOps access token on demand from its own `az` login and
# hands it to callers. GET-only: a worker can READ/consume the credential but can
# never push or mutate it. Control-token gated (workers already carry HIVE_TOKEN)
# so it is not exposed to the low-privilege read-only/dashboard token.
_ADO_RESOURCE = '499b84ac-1321-427f-aa17-267ca6975798'  # Azure DevOps app id
_SECRET_LOCK = threading.Lock()
_SECRET_CACHE: dict = {}   # name -> (payload, expiry_epoch)
_NUGET_FEEDS = [
    {'name': 'OneIdentity',
     'url': 'https://pkgs.dev.azure.com/1id/_packaging/OneIdentity%40Local/nuget/v3/index.json'},
    {'name': 'Starling2FA',
     'url': 'https://1id.pkgs.visualstudio.com/_packaging/2FA@Release/nuget/v3/index.json'},
]


def _mint_ado_token() -> dict:
    """Mint a fresh Azure DevOps access token via the server's `az` login.

    Cached in-process until 5 min before expiry so bursts of worker builds don't
    each shell out to az. Raises RuntimeError on failure (surfaced as 502)."""
    now = time.time()
    with _SECRET_LOCK:
        cached = _SECRET_CACHE.get('nuget')
        if cached and (cached[1] - now) > 300:
            return cached[0]
        az = shutil.which('az') or '/usr/bin/az'
        try:
            out = subprocess.run(
                [az, 'account', 'get-access-token', '--resource', _ADO_RESOURCE,
                 '-o', 'json'],
                capture_output=True, text=True, timeout=45)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f'az invocation failed: {e}')
        if out.returncode != 0:
            raise RuntimeError(f'az returned {out.returncode}: '
                               f'{out.stderr.strip()[-300:]}')
        tok = json.loads(out.stdout)
        payload = {
            'username': 'hive',            # any non-empty username works for ADO
            'password': tok['accessToken'],
            'feeds': _NUGET_FEEDS,
            'expiresOn': tok.get('expiresOn'),
            'tokenType': tok.get('tokenType', 'Bearer'),
        }
        exp = float(tok.get('expires_on') or (now + 2400))
        _SECRET_CACHE['nuget'] = (payload, exp)
        return payload


@app.get('/secret/<name>')
def secret(name):
    """Serve a named build credential. Control-token gated (see _guard).

    Currently only `nuget` is supported: returns a freshly minted Azure DevOps
    access token plus the feed URLs it authenticates, for the worker to write
    into a NuGet.Config `packageSourceCredentials` (or pass via RestoreConfigFile)."""
    if name != 'nuget':
        return jsonify(error='unknown secret'), 404
    try:
        return jsonify(_mint_ado_token())
    except RuntimeError as e:
        log.warning('secret/nuget mint failed: %s', e)
        return jsonify(error='mint failed', detail=str(e)), 502


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
    # lane/id are interpolated into the generated PowerShell/sh (only caps is
    # shell-quoted in bootstrap.py). Reject anything outside a safe charset so a
    # value like  x'; <cmd>;'  can't break out of the single-quoted literal and
    # inject into the script. Defense-in-depth — /bootstrap is control-token
    # gated — but cheap and closes the hole regardless.
    _SAFE = re.compile(r'^[A-Za-z0-9._#-]+$')
    if not _SAFE.match(lane) or (wid and not _SAFE.match(wid)):
        return Response('# error: lane/id must match [A-Za-z0-9._#-]+\n',
                        status=400, mimetype='text/plain')
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
    html = html.replace('%JOIN_ORIGIN%', join_origin())
    # Operator chose to display the control token on the Add-a-worker panel for
    # easy copy (homelab, host-only LAN trust). It is shown masked with a reveal
    # toggle; the join one-liner still only carries it once the operator clicks
    # "Use this token"/"Set token". NOTE: this DOES place the control token in
    # the page source — acceptable only because /dashboard is host-only LAN.
    html = html.replace('%CTL_TOKEN%', TOKEN)
    return Response(html, mimetype='text/html')


@app.post('/claim')
def claim():
    body = request.get_json(force=True, silent=True) or {}
    worker_id = body.get('worker_id')
    lane = body.get('lane')
    caps = body.get('caps')
    code_version = body.get('code_version')
    install_dir = body.get('install_dir')
    hostname = body.get('hostname')
    python_exe = body.get('python_exe')
    if not worker_id:
        return jsonify(error='worker_id required'), 400

    host = request.remote_addr
    # Register/refresh as idle up-front so an idle worker (which spends its life
    # long-polling here) shows on the dashboard even before it claims anything.
    q.upsert_worker(worker_id, lane=lane, caps=caps, status='idle',
                    current_task=None, host=host, code_version=code_version,
                    install_dir=install_dir, hostname=hostname,
                    python_exe=python_exe)

    # Version-driven self-update signal (rides every claim, incl. the empty 204
    # long-poll return): advertise the fleet's current code version and, if the
    # operator has requested this specific worker update, its target. The worker
    # acts on these only when idle (this branch), never mid-task.
    def _stamp(resp):
        resp.headers['X-Hive-Code-Version'] = str(CODE_VERSION)
        upd = q.get_worker_update(worker_id)
        if upd is not None:
            resp.headers['X-Hive-Update-To'] = str(upd)
        return resp

    deadline = time.time() + CLAIM_LONGPOLL_S
    while True:
        try:
            task = q.claim_next(worker_id, lane, worker_caps=caps)
        except sqlite3.OperationalError as exc:
            # Sc3 (#12): claim_next already retries transient locks; if it still
            # fails, tell the worker to back off (503, retryable) instead of 500.
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                return jsonify(error='queue busy, retry'), 503
            raise
        if task is not None:
            q.upsert_worker(worker_id, lane=lane, caps=caps, status='busy',
                            current_task=task.id, host=host,
                            code_version=code_version, install_dir=install_dir,
                            hostname=hostname, python_exe=python_exe)
            return _stamp(jsonify(
                id=task.id, branch=task.branch, base_ref=task.base_ref,
                prompt=task.prompt, scope=task.scope, attempts=task.attempts,
                model=task.model, lease_token=task.lease_token,
                kind=task.kind, review_of=task.review_of,
                review_branch=task.review_branch,
                repo=task.repo,
                disable_mcp=task.disable_mcp,
            )), 200
        if time.time() >= deadline:
            return _stamp(Response('', status=204))
        time.sleep(CLAIM_POLL_STEP + random.uniform(0, 0.5))   # jitter vs herd


@app.post('/heartbeat')
def heartbeat():
    body = request.get_json(force=True, silent=True) or {}
    # progress: accept either a pre-serialised JSON string or an inline object
    # (the worker sends a string; be lenient for manual/other callers).
    prog = body.get('progress')
    if prog is not None and not isinstance(prog, str):
        try:
            prog = json.dumps(prog)
        except (TypeError, ValueError):
            prog = None
    alive = q.heartbeat(body['task_id'], body['worker_id'],
                        lease_token=body.get('lease_token'),
                        progress_json=prog)
    # A heartbeat means the worker is busy on this task — keep its liveness fresh.
    q.upsert_worker(body['worker_id'], status='busy',
                    current_task=body['task_id'], host=request.remote_addr)
    return jsonify(alive=alive), 200


@app.post('/complete')
def complete():
    b = request.get_json(force=True, silent=True) or {}
    # Accept usage as either a pre-serialised JSON string or an inline object.
    raw_usage = b.get('usage_json') or b.get('usage')
    if isinstance(raw_usage, dict):
        import json as _json
        raw_usage = _json.dumps(raw_usage)
    status = q.complete_task(
        b['task_id'], b['worker_id'], int(b.get('exit_code', 1)),
        commit_hash=b.get('commit_hash'), turns=b.get('turns'),
        cost_pr=b.get('cost_pr'), log_path=b.get('log_path'),
        log_tail=b.get('log_tail'), verified=b.get('verified'),
        verified_base=b.get('verified_base'), error=b.get('error'),
        lease_token=b.get('lease_token'),
        usage_json=raw_usage,
    )
    return jsonify(status=status), 200


@app.post('/add')
def add():
    b = request.get_json(force=True, silent=True) or {}
    try:
        q.add_task(
            b['id'], b['prompt'], branch=b.get('branch'),
            base_ref=b.get('base_ref', q.DEFAULT_BASE_REF), worker=b.get('worker'),
            requires=b.get('requires'), scope=b.get('scope'),
            priority=int(b.get('priority', 5)),
            complexity=(int(b['complexity']) if b.get('complexity') is not None else None),
            model=b.get('model'),
            deps=b.get('deps'), max_attempts=int(b.get('max_attempts', 3)),
            review_max=int(b.get('review_max', 0)),
            repo=b.get('repo', 'lab.git'),
            disable_mcp=b.get('disable_mcp'),
            kind=b.get('kind', 'work'),
        )
    except KeyError as exc:
        return jsonify(error=f'missing required field: {exc.args[0]}'), 400
    except sqlite3.IntegrityError as exc:
        # Duplicate task id (PK) or branch (UNIQUE) — a conflict, not a 500. (U4)
        return jsonify(error=f'task already exists (id or branch conflict): {exc}'), 409
    return jsonify(id=b['id']), 201


@app.post('/task/<author_id>/review')
def review_verdict(author_id):
    """Apply a reviewer's verdict to an author task awaiting review.

    Control-token gated (NOT in READONLY_PATHS, so the _guard before_request
    already forces the control token — no auth change needed). The worker running
    a `kind='review'` task POSTs its parsed HIVE_REVIEW_VERDICT.json here instead
    of going through /complete; apply_review_verdict promotes the author to 'done'
    (APPROVE), bounces it to 'pending' with the findings appended (REQUEST_CHANGES
    under the round cap), or parks it as 'needs_attention' (at the cap)."""
    b = request.get_json(silent=True) or {}
    verdict = b.get('verdict')
    review_task_id = b.get('review_task_id')
    if not verdict or not review_task_id:
        return jsonify(error='verdict and review_task_id required'), 400
    raw_usage = b.get('usage_json') or b.get('usage')
    if isinstance(raw_usage, dict):
        import json as _json
        raw_usage = _json.dumps(raw_usage)
    result = q.apply_review_verdict(author_id, verdict, b.get('findings', ''),
                                    review_task_id, usage_json=raw_usage)
    return jsonify(result=result, author=author_id), 200


@app.post('/cancel')
def cancel():
    b = request.get_json(force=True, silent=True) or {}
    return jsonify(cancelled=q.cancel_task(b['task_id'])), 200


@app.post('/worker/remove')
def worker_remove():
    b = request.get_json(silent=True) or {}
    worker_id = b.get('worker_id')
    if not worker_id:
        return jsonify(error='worker_id required'), 400
    removed = q.remove_worker(worker_id)
    return jsonify(removed=removed, worker_id=worker_id), 200


@app.post('/worker/update')
def worker_update():
    """Request a version-driven self-update. Sets workers.update_to on one worker
    (worker_id) or all (all=true); the worker self-updates on its next idle claim.
    version defaults to the server's current CODE_VERSION. (mode A / halctl update)"""
    b = request.get_json(force=True, silent=True) or {}
    version = b.get('version', CODE_VERSION)
    try:
        version = int(version)
    except (TypeError, ValueError):
        return jsonify(error='version must be an integer'), 400
    worker_id = b.get('worker_id')
    if not worker_id and not b.get('all'):
        return jsonify(error='worker_id or all=true required'), 400
    n = q.set_worker_update(version, worker_id=None if b.get('all') else worker_id)
    return jsonify(requested=n, version=version,
                   worker_id=worker_id if not b.get('all') else 'ALL'), 200


@app.post('/workers/prune')
def workers_prune():
    b = request.get_json(silent=True) or {}
    stale_after_s = b.get('stale_after_s', q.WORKER_STALE_S)
    removed = q.prune_workers(stale_after_s)
    return jsonify(removed=removed, count=len(removed)), 200


@app.post('/requeue')
def requeue():
    """Re-enqueue a parked task (needs_attention|dead|cancelled) -> pending. (U1)"""
    b = request.get_json(force=True, silent=True) or {}
    task_id = b.get('task_id')
    if not task_id:
        return jsonify(error='task_id required'), 400
    ok = q.requeue_task(task_id, reset_attempts=bool(b.get('reset_attempts', True)))
    return jsonify(requeued=ok), 200


@app.get('/status')
def status():
    snap = q.status_snapshot()
    snap['code_version'] = CODE_VERSION   # server's advertised worker code version
    return jsonify(snap), 200


@app.get('/metrics')
def metrics():
    """Fleet counters since server start (claims, merges, parks, dead-letters,
    …) plus current per-status task counts. Read-only (U5 #19)."""
    snap = q.status_snapshot()
    return jsonify(counters=hive_log.snapshot(), task_counts=snap['counts']), 200


@app.get('/logs/<task_id>')
def logs(task_id):
    """Return the stored tail of a task's worker copilot log as plain text, so
    failure triage no longer requires SSHing the worker (U2 #16). 404 if the
    task is unknown or produced no log."""
    tail = q.get_log_tail(task_id)
    if tail is None:
        return jsonify(error=f'no log for task {task_id}'), 404
    return Response(tail, mimetype='text/plain; charset=utf-8')


@app.get('/task/<task_id>')
def task_detail(task_id):
    """Full detail for one task (read-only), backing the dashboard's per-task
    popup: full prompt, state, timestamps (created/claimed/started/done,
    heartbeat), attempts, deps, worker, model, error. 404 if unknown."""
    task = q.get_task(task_id)
    if task is None:
        return jsonify(error=f'no task {task_id}'), 404
    return jsonify(task)


def resolve_bind_addrs(override, default_lab_ip='192.168.100.1'):
    """Addresses the control plane listens on (S2 #5).

    Default (override unset): 127.0.0.1 for HAL-local access + the lab-net NIC
    for workers, but NOT the corp interface, so the queue/git transport is not
    exposed to the wider corporate network. Override with HIVE_BIND /
    GIT_HTTP_BIND (comma-separated; e.g. '0.0.0.0' to bind all). The lab IP is
    HIVE_LAB_IP (default 192.168.100.1). An address absent on the host is
    skipped at bind time, so this never hard-fails without the lab NIC."""
    if override:
        return [h.strip() for h in override.split(',') if h.strip()]
    lab = os.environ.get('HIVE_LAB_IP', default_lab_ip).strip()
    addrs = ['127.0.0.1']
    if lab and lab not in addrs:
        addrs.append(lab)
    return addrs


def build_ssl_context():
    """Return an ssl.SSLContext when HIVE_TLS_CERT and HIVE_TLS_KEY are both set,
    else None (plain HTTP). Terminates TLS so control/RO tokens no longer transit
    in clear text (issue #8 / S5). Use gen-lab-ca.sh to mint the cert/key."""
    cert = os.environ.get('HIVE_TLS_CERT')
    key = os.environ.get('HIVE_TLS_KEY')
    if not (cert and key):
        return None
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    return ctx


def main():
    q.init_db()
    q.ensure_lab_repo()
    stop = threading.Event()
    threading.Thread(target=q.reaper_loop, args=(stop,), daemon=True).start()
    threading.Thread(target=q.merge_loop, args=(stop,), daemon=True).start()
    from werkzeug.serving import make_server
    addrs = resolve_bind_addrs(os.environ.get('HIVE_BIND'))
    port = int(os.environ.get('HIVE_PORT', '5001'))
    ssl_ctx = build_ssl_context()
    scheme = 'https' if ssl_ctx else 'http'
    log.info('queue REST — reaper + merge-loop running')
    log.info('control token loaded (add/cancel/claim authorized)')
    log.info('read-only token (dashboard /status,/metrics,/logs) -> %s', RO_TOKEN_FILE)
    if not ssl_ctx:
        log.warning('TLS OFF — set HIVE_TLS_CERT/HIVE_TLS_KEY to encrypt (issue #8)')
    servers = []

    def _try_bind(addr):
        """Bind+serve one address. Returns True on success, False if the address
        is not assigned on the host yet (EADDRNOTAVAIL) or otherwise unbindable."""
        try:
            # threaded=True so long-poll /claim calls don't block reaper/others.
            srv = make_server(addr, port, app, threaded=True, ssl_context=ssl_ctx)
        except (OSError, SystemExit) as exc:
            # werkzeug turns a bind failure into a printed msg + SystemExit(1),
            # stdlib would raise OSError -- tolerate both so an absent NIC is skipped.
            log.warning('skip bind %s:%s (%s)', addr, port, exc or 'bind failed')
            return False
        servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info('listening on %s://%s:%s', scheme, addr, port)
        return True

    pending = [a for a in addrs if not _try_bind(a)]
    if not servers and not pending:
        raise SystemExit(f'[server] FATAL: could not bind any of {addrs}:{port}')

    # I2: self-heal the recurring "loopback-only" bug. The lab NIC (virbr1,
    # 192.168.100.1) is frequently not yet assigned when this unit starts (it is
    # brought up by libvirtd, which races network-online.target), so its bind is
    # skipped and the control plane otherwise serves loopback-only FOREVER — all
    # remote workers see "hal not responding" and Restart=on-failure never fires
    # because nothing crashed. Retry any skipped address in the background until
    # the bridge comes up, so remote workers reconnect with no manual restart.
    # git_http_server already does this; this ports the same proven loop here.
    if pending:
        def _rebind_loop(addrs_pending):
            remaining = list(addrs_pending)
            while remaining and not stop.is_set():
                stop.wait(5)
                if stop.is_set():
                    break
                remaining = [a for a in remaining if not _try_bind(a)]
            if not remaining:
                log.info('all expected addresses now bound (%r:%s)', addrs, port)
        threading.Thread(target=_rebind_loop, args=(pending,), daemon=True).start()
        log.warning('%d address(es) not yet assigned %r — retrying every 5s '
                    'until bound (no manual restart needed)', len(pending), pending)
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for srv in servers:
            srv.shutdown()


if __name__ == '__main__':
    main()
