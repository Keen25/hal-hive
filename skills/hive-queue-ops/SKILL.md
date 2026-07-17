---
name: hive-queue-ops
description: >-
  Use when operating, debugging, or extending the HAL "live hive" — the
  pull-persistent Phase 9 task queue that dispatches Copilot CLI coding tasks to
  distributed workers (mini-HALs) and merges results into ~/hive/lab.git. Covers
  halctl / queue_server / worker_loop usage, the SQLite queue schema and task
  state machine, the claim/lease/heartbeat/reaper model, the rebase→verify→merge
  loop, REST + token auth, systemd/scheduled-task launch, ar1 lane wiring over
  WinRM/HTTP, and the sharp edges found in testing. USE FOR: "add a hive task",
  "why is a task stuck / needs_attention", "start the hive", "wire a worker",
  "queue status". DO NOT USE FOR: the legacy synchronous dispatch_dag.py, ARS
  product code, or ADO/Salesforce work.
---

# HAL Live Hive — Queue Operations

The hive is **pull-persistent**: a SQLite queue (`~/hive/queue.db`) is the source
of truth. Workers self-drive (claim → run `copilot` → push → complete). HAL's
interactive session is a live control plane. Nothing blocks HAL while the fleet runs.

Design/history: gist `Keen25/08d90e38b9a9aad47120ef8e8b38036b`, files
`hal-phase9-built.md` (shipped), `hal-phase9.1-hardening.md` (design rationale).

## Files (all in `~/hive/`)

| File | Role |
|------|------|
| `hive_queue.py`   | Core lib — schema, claim, heartbeat, complete, reaper, merge_loop, `ensure_lab_repo`, status/control helpers. Import it; don't reimplement. |
| `queue_server.py` | Flask REST + bearer auth + long-poll `/claim`; starts reaper + merge-loop threads. **Only** interface for Windows workers. |
| `worker_loop.py`  | Self-driving worker (local or remote-over-HTTP). |
| `halctl.py`       | HAL control CLI: `add / status / cancel / prio / init`. |

- Queue DB: `~/hive/queue.db` (WAL). Token: `~/.hive/worker.token` (0600, auto-gen).
- Bare merge target: `~/hive/lab.git` (default branch `master`).
- **All DB access is HAL-local.** Windows workers NEVER open queue.db directly
  (SQLite locking is broken over SMB/NFS) — they go through the REST server.

## Quick start

```bash
python3 ~/hive/halctl.py init                     # queue.db + seed lab.git root commit
python3 ~/hive/queue_server.py                    # control plane on 0.0.0.0:5001
TOKEN=$(cat ~/.hive/worker.token)
python3 ~/hive/worker_loop.py --worker-id hal#1 --lane hal \
        --queue http://127.0.0.1:5001 --token "$TOKEN"
```

Drive it (non-blocking) from HAL's session:

```bash
python3 ~/hive/halctl.py add "add binary_search.py" --priority 1 --worker hal
python3 ~/hive/halctl.py add "build on Windows" --require os:windows --require msbuild
python3 ~/hive/halctl.py add "write tests"  --dep task-abc123
python3 ~/hive/halctl.py status
python3 ~/hive/halctl.py cancel task-abc123
python3 ~/hive/halctl.py prio  task-abc123 1
```

`--worker` pins a task to one exact lane. `--require` (repeatable) declares
capability tags a worker must ALL advertise to claim it. `--dep` is repeatable.
Task IDs auto-generate (`task-<hex>`) unless you pass `--id`.

## Capability-based claiming (task requirements ↔ worker caps)

A task can declare **requirements** (`--require`) and a worker advertises
**capabilities** (`--cap` + auto-detected). A worker may claim a task only when
`requires ⊆ caps`. This is how a Linux worker is prevented from picking up a
Windows build.

- **Worker auto-caps (Phase 9.2 self-probe):** `worker_loop` now delegates to
  `hive_caps.probe_caps()`, which `which()`-detects the toolset and adds
  `build:dotnet`, `has:docker`, `role:builder|tester`, etc. on top of
  `os:/arch:/platform:/name:`. Add more with `--cap` / `HIVE_CAPS`. Restart a
  worker after installing a toolchain to re-advertise. Example hal lane now:
  `arch:x86_64 build:dotnet build:node has:git has:python name:hal os:linux platform:posix role:builder`.
- **Auto-tagging (Phase 9.2):** don't hand-remember `--require`. Use the
  **`hive-task-intake` skill** — it runs `halctl classify "<prompt>"`, confirms
  the inferred tags, asks when ambiguous, then `halctl add --require …`. Critical
  for ARS: `hal` HAS `dotnet`, so an ARS build needs `os:windows` too (the
  classifier adds it) or it'd build on Fedora and fail. See
  `hal-phase9.2-capability-intake.md` + `hive_caps.py` in the gist.
- **Tags are opaque, lowercased, namespaced by convention** (`os:windows`,
  `msbuild`, `name:ar1`). Matching is a case-insensitive subset test.
- **`--worker` (exact pin) and `--require` (caps) are independent AND-ed gates.**
  Use the pin for "this specific box", requirements for "any box that can do X".
- A task whose requirements no running worker satisfies stays `pending`
  indefinitely (by design) — `halctl status` shows its `requires=[...]`.

Example — a Windows-only build:
```bash
python3 ~/hive/halctl.py add "msbuild ARS solution" --require os:windows --require msbuild
# a Windows worker: worker_loop.py --lane ar1 --cap msbuild  (auto-adds os:windows)
```

## Task state machine

```
pending → claimed → running → done → verifying → merged
   ▲         │         │        │         │
   │         │         │        │         ├─► needs_attention (rebase/verify fail)
   │         └─────────┴────────┘         │
   │            (lease expired → reaper)  │
   └───────── (fail → backoff) ───────────┘
                    │
                    └─► dead  (attempts ≥ max_attempts)
   cancelled  (user, any pre-merge state)
```

- **Claim** (`hive_queue.claim_next`): atomic `BEGIN IMMEDIATE`; a task is claimable
  when `pending`, past its `not_before` backoff, **every dependency is `merged`**
  (not merely `done`), its `worker` pin (if any) equals this lane, AND the task's
  `requires` set ⊆ this worker's `caps`. Ordered by `priority − min(attempts,3)`
  (aging). Capability subset matching is done in Python over the priority-ordered
  candidates (kept atomic by the held write lock). Writes a 15-min lease.
- **Heartbeat** (60s): renews the lease. Returns `alive=False` if the row was reaped
  or reassigned → the worker abandons the in-flight run.
- **Reaper** (30s): requeues `claimed`/`running` tasks whose lease expired;
  dead-letters at `max_attempts`.
- **Merge-loop** (2s, single-threaded, `done_at ASC`): for each mergeable `done`
  task — rebase branch onto the LIVE `base_ref` in a scratch worktree → build/test
  gate → fast-forward `base_ref` → `merged`. Conflicts / verify failures park as
  `needs_attention` (a human queue — never silent).

## REST API (bearer token in `Authorization: Bearer <token>`)

```
POST /claim     {worker_id, lane?, caps?}  -> 200 task | 204 empty  (long-polls 25s)
POST /heartbeat {task_id, worker_id}      -> {alive: bool}
POST /complete  {task_id, worker_id, exit_code, commit_hash?, log_path?, error?}
POST /add       {id, prompt, ...}         -> 201
POST /cancel    {task_id}                 -> {cancelled: bool}
GET  /status                              -> {counts, tasks}
GET  /health                              -> {ok: true}   (no auth)
```

Env: `HIVE_PORT` (5001), `HIVE_BIND` (0.0.0.0), `HIVE_TOKEN`, `HIVE_VERIFY_CMD`,
`HIVE_VERIFY_OPTIONAL`, `HIVE_QUEUE_URL`, `HIVE_LAB_GIT`.

## The verify gate

The merge-loop runs `HIVE_VERIFY_CMD` (shell, cwd `~/hive`) before fast-forwarding.
Exit 0 → merge; non-zero → park `needs_attention` with the last output lines. If
`HIVE_VERIFY_CMD` is unset, verify is skipped (lab default). Point it at a real ARS
build/test on `ar1` to gate merges.

## Debugging stuck tasks

- `halctl status` shows per-task state + `needs_attention` reason.
- **`needs_attention: rebase conflict`** → a genuine content conflict on the branch
  vs live base. Resolve the branch manually and re-push, or cancel + re-add.
- **Task stuck `claimed`/`running`** → worker died mid-run; the reaper requeues it
  within 30s (watch `attempts`). If it hits `dead`, the run failed `max_attempts`
  times — inspect `error` and the worker's `log_path`.
- **Everything returns 204 / nothing claimed** → all pending tasks are dep-gated on
  unmerged deps, pinned to an absent lane, gated by `requires` no running worker
  satisfies, or in `not_before` backoff. Check `halctl status` for `requires=[...]`
  / `pin=` lines and confirm a worker advertising those caps is actually running.

## Sharp edges (learned the hard way — do not regress)

- **`ensure_lab_repo()` MUST run before any worker.** An empty `lab.git` lets a
  worker branch off an unborn HEAD → orphan root commit that can never rebase. The
  fix seeds an empty root commit on `master`. `halctl init` and the server both call it.
- **Never write the copilot run log inside the git clone.** `worker_loop` puts the
  JSONL log in a temp file OUTSIDE the run dir; a log inside the clone gets
  `git add -A`'d and makes every task conflict on the same file.
- **A non-zero `git rebase` exit is not always a conflict.** The scratch rebase
  worktree needs a git identity (`GIT_AUTHOR_*/GIT_COMMITTER_*` env) or it aborts
  with "Committer identity unknown" — which must NOT be parked as a conflict.
- **Connection invariant:** every process opening `queue.db` uses `hive_queue.connect`
  (WAL + `busy_timeout=5000` + FK). Don't `sqlite3.connect` the file directly.

## Running as services

- HAL host: `queue_server.service` (control plane) + `hive-worker@.service`
  (templated per lane, e.g. `hive-worker@hal.service`). See `~/hive/systemd/`.
- Windows workers (ar1): `worker_loop.py` runs as a **WinSW Windows service**
  (`hive-worker-<lane>.exe`, provisioned by `bootstrap.py`) under the
  least-privilege local **`.\svc-hive`** account, `<onfailure restart>` on exit
  (the retired scheduled-task/`run-worker.ps1` path as `fodor\a` is gone).
  It reaches HAL's queue over HTTP `http://<HAL>:5001` with the shared token, and
  clones/pushes over smart-HTTP (`:8418`). Dispatch to ar1 from Linux is
  always via WinRM (pywinrm), never SSH. Per-task copilot ceiling =
  `COPILOT_TIMEOUT_S` in the WinSW XML (bootstrap seeds 5400s; ar1 currently
  2400s — an ARS full-solution build+test needs ~40 min, so keep this generous).

### ar1 scheduled-task launch recipe (one-shot `copilot` dispatch; verified 2026-07-03)

> **Not the worker daemon.** The persistent worker runs as the `hive-worker-ar1`
> **WinSW service** (above). This recipe is only for launching an *ad-hoc,
> one-shot* `copilot` run on ar1 (mini-HAL dispatch outside the queue).

Launching `copilot` on ar1 via a one-shot Scheduled Task is the ONLY reliable
way to survive the WinRM session (direct `run_ps` dies on long agentic runs;
`Start-Process`/detached also dies). Two non-obvious flags MUST both be set or
the run silently produces nothing:

1. **`/RU 'fodor\a' /RP '<pass>'` — stored credentials are mandatory.** Without
   them the task principal can only start in an interactive logon, which does
   not exist over WinRM. `schtasks /Run` then returns `SUCCESS` but the task
   never executes: `Last Run Time = 11/30/1999`, `Last Result = 267011`
   (`SCHED_S_TASK_HAS_NOT_RUN` / `0x41303`). A running task shows `267009`
   (`SCHED_S_TASK_RUNNING`); a clean finish shows `267011`→`0`.
2. **`copilot --allow-all` (NOT just `--allow-all-tools`).** `--allow-all-tools`
   authorises the tool but NOT file paths outside the CWD, so writes to e.g.
   `D:\tmp` are refused with *"Permission denied ... blocked by the environment's
   permission system, not the OS"* and the agent exits 0 having written nothing.
   `--allow-all` = `--allow-all-tools --allow-all-paths --allow-all-urls`.

Working invocation (creds sourced from `~/.hive/hive.env` → `$LAB_PASS`):

```powershell
schtasks /Create /TN hive_<id> /SC ONCE /ST 00:00 /RL HIGHEST /F `
  /RU 'fodor\a' /RP '<pass>' `
  /TR 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\tmp\<id>_run.ps1'
schtasks /Run /TN hive_<id>
```
The runner `<id>_run.ps1` invokes `& C:\npm-global\copilot.ps1 --allow-all
--autopilot -p $b` where `$b = Get-Content -Raw D:\tmp\<id>_brief.txt`. Brief =
single line, single-quotes only, NO embedded double-quotes (the `copilot.ps1`
shim errors "Invalid command format" on embedded `"`). Push brief+runner with
`winrm_push.py` (chunked base64). The `node.exe ... NativeCommandError` noise in
the run log is cosmetic CLIXML-on-stderr, not a failure — check the actual
output file and `EXIT 0`.

## Boundaries

DO NOT use this skill for: the legacy synchronous `dispatch_dag.py`, ARS product
code, ADO/Salesforce/PagerDuty work, or the Phase 10 voice layer.
