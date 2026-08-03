# HAL Swarm — live task queue

Pull-persistent SQLite task queue that dispatches Copilot CLI coding tasks to
distributed workers (mini-HALs) and merges results into `lab.git`. HAL's session
is a live control plane; nothing blocks while the fleet runs.

Design history + phase docs: gist `Keen25/08d90e38b9a9aad47120ef8e8b38036b`.

> **Scaling?** ~10 workers is feasible today with no redesign; the ceiling is
> the serialized merge+verify loop, not worker count. See
> [Scaling & known limits](#scaling--known-limits-honest-take).

## Layout

| File | Role |
|------|------|
| `hive_queue.py` | queue core — schema, atomic `claim` (dep-gate + lease + capability match), reaper, merge-loop |
| `queue_server.py` | REST control plane (bearer-token auth), reaper + merge-loop threads |
| `worker_loop.py` | self-driving worker: claim → run `copilot` → push → complete; self-probes its caps; **version-driven self-update** |
| `halctl.py` | control CLI: `add` (`--review N`, `--repo`, `--disable-mcp`) / `status` / `workers` / `update` / `cancel` / `requeue` / `prio` / `classify` / `promote` (mirror-repo → ADO PR) |
| `hive-console.sh` | one-window tmux operator console: **sessions · queue · live Copilot CLI · deliveries** (see [Operator console](#operator-console-delivery--resume)) |
| `hive_sessions.py` | single-pane status of all local Copilot CLI sessions; flags idle/`WAITING` ones |
| `hive_watch.py` | delivery→resume watcher: `register`/`wait`/`run` — pushes offloaded-task completions back into a session inbox |
| `hive_queue_active.py` | compact live view of actionable queue tasks (claimed/pending/running/needs_attention) |
| `hive_deliveries.py` | robust follower of `inbox/*.jsonl` delivery logs (console DELIVERIES pane) |
| `hive_caps.py` | capability vocabulary — `probe_caps()` (worker) + `classify_prompt()` (intake) |
| `hive_version.py` | single source of truth for the worker code version (`CODE_VERSION`); bump to release a fleet self-update |
| `classifier_rules.d/` | external classifier rule packs (product-specific `requires` rules; ships `activeroles.json`) |
| `gen-lab-ca.sh` | mint the lab CA + swarm server cert (TLS, issue #8) |
| `trust-lab-ca.ps1` | trust the lab CA on a Windows worker (ar1) |
| `dispatch_dag.py` | legacy synchronous DAG dispatcher (kept, not the live path) |
| `winrm_run.py` / `winrm_push.py` | ar1 (Windows) WinRM helpers |
| `ad-overload/` | AD load-generator utility |
| `systemd/` | `--user` units: queue-server, `worker@`, git-daemon |
| `briefs/` | per-task spec inputs |
| `skills/` | Copilot CLI skills (`hive-queue-ops`, `hive-task-intake`) — backup of `~/.agents/skills/`; the live copies are what the agent loads |
| `tests/` | state-machine + Flask smoke tests for the queue core and peer-review loop |

## Run (systemd `--user`)

```bash
systemctl --user status  hive-queue-server hive-worker@hal hive-git-daemon
python3 halctl.py add "<prompt>" --require build:dotnet   # or use the hive-task-intake skill
python3 halctl.py status
python3 halctl.py workers                                 # per-worker quirks: host, install dir, os/build caps, code version
```

## Operator console (delivery → resume)

One tmux window instead of juggling separate terminal tabs. Four panes, all
read-only except the CLI: **sessions** (which chat is `WAITING`) · **queue**
(active tasks) · **live `copilot`** (chat from inside the console) ·
**deliveries** (offloaded-task completions land here).

```bash
./hive-console.sh                       # attach (creates it if needed); Ctrl-b d to detach
./hive-console.sh --kill                # tear down
HIVE_SESSION_ID=<sid> ./hive-console.sh # also write completions into that session's inbox
# CLI pane knobs: HIVE_CLI_ARGS=--continue  HIVE_CLI_DIR=/repo  HIVE_CLI_CMD=bash
```

**Async-offload workflow** — offload a self-contained task, keep working, pick
up the thread when the worker delivers:

```bash
TID=$(python3 halctl.py add "<defect/build/verify task>" --require build:dotnet | awk '/^\+ /{print $2; exit}')
python3 hive_watch.py register "$TID"                     # add to this session's watchlist
python3 hive_watch.py wait "$TID" &                       # detached: its EXIT is the wake signal
#   ... continue interactive work ...
#   on completion: delivery written to inbox/<sid>.jsonl + session.db inbox_entries,
#   and the DELIVERIES pane prints it.
```

> Note: the native `inbox_entries` table does **not** auto-surface to an agent;
> the `wait` process exiting (or the `run` loop) is what signals "pick up the
> thread". `hive_sessions.py` alone (no console) also works: `--watch` for a
> live table, `--json` for scripting.

## Secrets & state (NOT in this repo)

- Token + lab creds live in `~/.hive/` (`worker.token`, `hive.env` with
  `HIVE_TOKEN`, `LAB_PASS`). Scripts read `LAB_PASS` from the environment — source
  `~/.hive/hive.env` first, or they exit with a clear error.
- `queue.db`, `lab.git/`, and worker clones are runtime state (gitignored).

## Worker self-update + registry (version-driven, in-band)

Workers update themselves — no WinRM, no manual file copy. The upgrade is
**worker-native daemon code** (download → verify → swap → restart), *not* a
copilot task, so it is not subject to the agent safety model that refuses
task-driven self-modification.

**How it works**

- `hive_version.py` holds `CODE_VERSION` (a monotonic int) — the single source
  of truth. `GET /version` serves it plus a per-file **sha256 manifest**.
- Every `/claim` response (including the empty 204 long-poll return) carries
  `X-Hive-Code-Version` (the fleet's current version) and, when an update is
  requested for that worker, `X-Hive-Update-To`. Zero extra polling.
- When **idle**, a worker that sees a target it hasn't reached fetches the
  allowlisted assets, verifies each against the manifest sha256, **compile-checks**
  every `.py` before swapping, backs up the old file (`*.bak-<ts>`), swaps, and
  exits with code `42`. WinSW `onfailure`/systemd `Restart` respawn it into the
  new code. A hash mismatch or syntax error **aborts without touching disk** — a
  bad push can never brick a worker on restart.

**Two triggers** (same primitive):

| Mode | Trigger | Use |
|------|---------|-----|
| **A — command** | `halctl update --worker <lane>` or `--all` sets `workers.update_to`; the worker acts on its next idle claim. | operator-gated, canary-able (default) |
| **B — auto** | worker with `HIVE_AUTO_UPDATE=1` self-updates whenever the advertised version is newer. | zero-touch, forward-only, opt-in per box |

```bash
# edit worker_loop.py etc., then:
#   1. bump CODE_VERSION in hive_version.py
#   2. restart hive-queue-server  (re-reads /version + manifest)
python3 halctl.py update --all            # roll to the whole fleet
python3 halctl.py update --worker sql1#1  # or canary one lane first
python3 halctl.py workers                 # watch update_to clear as each reaches the target
```

**Worker registry** — each worker self-reports its identity on every claim so the
control plane always knows **where** it runs (prevents "pushed to the wrong copy"
blips, e.g. a worker on `D:\hive` while an operator assumes `C:\hive`):

| Field | Meaning |
|-------|---------|
| `code_version` | the worker's running `CODE_VERSION` (⚠ badge when it differs from the server) |
| `install_dir` | directory the worker actually runs from |
| `hostname` / `host` | the box's own machine name / its address as seen by the server |
| `python_exe` | interpreter running the worker |
| `caps` | probed toolset (`os:`, `build:`, `has:`, `role:`, `arch:`) |

`halctl workers` prints these per worker; the dashboard shows a compact
`os · build-tools` line with the full identity on hover.

**Live per-task progress** — a busy worker piggybacks a compact in-flight
snapshot on its 60s heartbeat, tailed cheaply from the live copilot JSONL log
(no extra process): `last_tool` (tool + a legible arg + turn), `last_msg` (latest
assistant text), `turn`, `elapsed_s`, and `phase` (`working` → `finishing` once
the terminal result frame appears). `halctl status` and the dashboard surface it
so a long run is visibly progressing rather than an opaque `running`. Cleared on
completion and reset to `NULL` on re-claim.

## Task routing across repos + ADO promote (`--repo` / `halctl promote`)

A task defaults to the `lab.git` sandbox, but `--repo <name>` points its
clone/branch/push at a different central bare repo served on `:8418` (e.g.
`--repo ActiveRoles.git` for an ARS source task), so the change lands centrally
and stays reviewable before it ever reaches ADO.

```bash
python3 halctl.py add "<ARS change>" --repo ActiveRoles.git --require os:windows --complexity 3
python3 halctl.py promote <task-id>          # DRY-RUN: fetch the delivered branch, show diff + commits
python3 halctl.py promote <task-id> --yes    # APPLY: cherry-pick onto the ADO PR branch and push
```

- **`promote` is mirror-repo only** — a `lab.git` task has nothing to promote; it
  errors out. It never merges into the mirror itself, only cherry-picks the
  task's commits onto the live **ADO PR branch** (`--pr-branch`, default the
  task's `base_ref`) and pushes.
- **Dry run by default** — nothing is pushed without `--yes`; `--show-diff`
  prints the full patch instead of just the diffstat.

## Automated peer-review loop (opt-in, `--review N`)

Add `--review N` to a task and its diff is reviewed by a **different** worker
before it merges. `N` is the max number of review rounds. No human in the loop.

```bash
python3 halctl.py add "<prompt>" --review 1     # one review round, then merge/park
```

State machine:

```
author succeeds ─(review_max>0)→ awaiting_review   (invisible to the merge loop)
     └ spawn_review → kind='review' task, pinned AWAY from the author's worker
          reviewer checks out the AUTHOR branch, POSTs a verdict:
            APPROVE          → author done → merges
            REQUEST_CHANGES  → author pending, findings appended → loops to cap
          at cap             → needs_attention (findings in error)
```

- **Fail-safe** — any verdict that is not exactly `APPROVE` is treated as
  REQUEST_CHANGES; a garbled/timed-out reviewer never auto-approves. Review tasks
  always close to `reviewed`, never `done`.
- **Anti-affinity** — `review_exclude_worker` keeps the author's own worker from
  claiming its review; the author's branch is threaded to the reviewer via the
  claim payload (`review_branch`, since `tasks.branch` is UNIQUE).
- **Graceful degrade** — on a single-worker fleet (no other online worker) the
  review is skipped and the author goes straight to `done`. If the only reviewer
  goes offline after a review is spawned, the reaper (`reap_stalled_reviews`)
  degrades the unclaimable review and promotes the author.
- **Cancel/requeue are review-aware** — cancelling an author cascades to its live
  review; cancelling a review releases the author to `needs_attention`;
  requeueing an author resets `review_round`.

Feature doc: gist `Keen25/33551ae5e45c698e33e9382422532141`.

## Usage metrics

The dashboard rollup surfaces **reliable** per-task Copilot signals — total API
time and lines changed (`codeChanges`). `premiumRequests` is intentionally **not**
shown: it is a hard constant (15) in Copilot CLI 1.0.68, not real billing. Review
passes capture usage too, so reviewing workers appear in the rollup, not just
authors.

## Classifier rule packs (intake `requires`, F3 #22)

`classify_prompt()` infers a task's `requires` tags. The engine ships only
**product-agnostic** build/platform rules (`.NET`, Go, Node, Rust, Python,
Docker, "on Windows"). **Product-specific** rules live in external JSON packs so
the classifier is reusable across products without editing code.

Sources (all optional, all additive — a bad rule is skipped, never fatal):

| Source | Purpose |
|--------|---------|
| `classifier_rules.d/*.json` next to `hive_caps.py` | bundled packs; ships `activeroles.json` (Active Roles/OI.Ldap → `os:windows build:dotnet`). Override the dir with `HIVE_CLASSIFIER_RULES_DIR`; set it empty to load none. |
| `HIVE_CLASSIFIER_RULES_FILE` (or `~/.hive/classifier_rules.json`) | one operator rule file |
| `HIVE_CLASSIFIER_RULES` | inline JSON list in the environment |

Each pack is a JSON list of rules:

```json
[{ "pattern": "\\bactive\\s*roles\\b", "tags": ["os:windows", "build:dotnet"],
   "reason": "why this maps here", "ignorecase": true }]
```

Reuse for another product = drop a new `*.json` in `classifier_rules.d/` (or
point `HIVE_CLASSIFIER_RULES_FILE` at it); no code change.

## Worker privilege model (S3 #6)

A queued prompt is **arbitrary code**: the worker runs `copilot` with broad tool
access (`--allow-all`) to actually build/edit/push. So anyone who can `POST /add`
(the control token — keep it secret, see S1) can execute code on every worker.
That is inherent to a code-execution worker; the design goal is to **cap the
blast radius**, not pretend it's zero.

Controls in place:

| Control | Effect |
|--------|--------|
| **Non-admin service account** | On Windows the worker service (`svc-hive`) runs as a **standard user**, never a local Administrator. `bootstrap.py` removes it from `Administrators` on (re)provision. A task can no longer own the box. |
| **Per-task sandbox dir** | All clones/logs/prompts live under `HIVE_WORK_DIR` (Windows: `C:\hive\work`, Full Control to `svc-hive` only, also the account's `TEMP`). Unset = system temp (Linux `--user` unit already runs unprivileged). |
| **Scoped copilot perms** | `HIVE_COPILOT_PERMS` overrides the copilot permission flags without a code change (default = the lab-proven `--no-ask-user --allow-all --autopilot`; a narrower `--allow-all-tools` blocks writes outside CWD and breaks tasks). Tighten per-deployment as your task mix allows. |
| **Scoped MCP toolset** | Suppress specific MCP servers for a task's copilot run without touching the global `~/.copilot/mcp-config.json`. Per task: `halctl add "<prompt>" --disable-mcp salesforce --disable-mcp pagerduty` (repeatable; also accepts a comma/space list). Per worker: `HIVE_DISABLE_MCP="salesforce pagerduty"` in the worker env (merged + de-duped with the task list). Names must match the **key** in `~/.copilot/mcp-config.json`. Emitted as `--disable-mcp-server <name>` flags; unset = load all configured servers (backward compatible). Review tasks inherit their author task's scope. **Fail-closed validation:** before launch the worker checks every requested name against `copilot mcp list --json`; an unknown/typo'd name (or an un-enumerable inventory) **fails the task** — a work task errors out, a review task returns `REQUEST_CHANGES` — rather than silently running with the wrong scope (the copilot CLI otherwise **ignores** an unknown `--disable-mcp-server` name and leaves that server loaded). |
| **Control-token gating + TLS** | Only the control token can `/add`/`/claim`/`/cancel`; enable TLS (below) so tokens aren't sniffable on the wire. |

**Residual, accepted risk:** a holder of the control token still gets
**standard-user** RCE on workers. On the host-only lab net with a secret control
token this is an accepted risk; on a shared/less-trusted network, additionally
scope `HIVE_COPILOT_PERMS`, run workers in throwaway VMs/containers, and rotate
the control token.

## TLS (optional, issue #8 / S5)

By default the control plane (`:5001`) and git transport (`:8418`) serve **plain
HTTP** — fine only on the trusted host-only lab net. To encrypt the tokens and git
packs, terminate TLS with a mini lab CA on the KVM host (HAL):

```bash
# 1. mint the CA + server cert (SANs auto-cover 127.0.0.1, 192.168.100.1, the
#    host's LAN IPs and hostname; add more as args)
./gen-lab-ca.sh                       # writes ~/.hive-ca/{ca,server}.{crt,key}

# 2. enable TLS on both servers (env-gated — unset both vars = plain HTTP)
export HIVE_TLS_CERT=~/.hive-ca/server.crt HIVE_TLS_KEY=~/.hive-ca/server.key
#    (add these to ~/.hive/hive.env so the systemd --user units pick them up)

# 3. trust the CA on HAL, then flip clients to https://
sudo cp ~/.hive-ca/ca.crt /etc/pki/ca-trust/source/anchors/hal-hive-ca.crt
sudo update-ca-trust
```

Windows worker (ar1): copy `~/.hive-ca/ca.crt` over, then from an **elevated**
PowerShell run `trust-lab-ca.ps1 -CaPath C:\hive\ca.crt` (imports into
`LocalMachine\Root` and sets git's `http.sslBackend=schannel`). Then point the
worker at `https://<HAL-ip>:5001` / `https://<HAL-ip>:8418/lab.git`.

> **Enabling TLS is a coordinated fleet cutover, not a per-box toggle.** Once
> `HIVE_TLS_CERT`/`HIVE_TLS_KEY` are set and the servers restart, `:5001`/`:8418`
> speak **https only** — every already-running worker still configured with
> `http://` URLs stops connecting until it is reconfigured (and trusts the CA).
> Plan to flip the servers and reconfigure existing workers together.

### Newly-joined workers over TLS (automatic)

`bootstrap.py` derives the scheme from HAL's TLS state, so **once TLS is on the
dashboard join one-liner needs no change** — new workers are wired for https with
zero manual trust step:

- The generated queue/git URLs become `https://…` automatically (scheme follows
  `_tls_enabled()`; force it independently with `HIVE_SCHEME=http|https`, e.g.
  when a reverse proxy terminates TLS).
- The lab **`ca.crt` is embedded in the script** and planted on the new box
  (`$DIR/ca.crt`). Linux/macOS point `REQUESTS_CA_BUNDLE` + `GIT_SSL_CAINFO` at
  it (no OS trust-store change, so no root needed); Windows imports it into
  `LocalMachine\Root` (for IWR + git-schannel) *and* sets `REQUESTS_CA_BUNDLE`
  (python-requests ignores the Windows store).
- The dashboard's **first** fetch of the script uses `-k` / a cert-skip callback
  (the fresh box does not trust the CA yet — TOFU on the lab net); every
  subsequent fetch and all worker traffic is fully CA-verified.

This closes the gap where flipping TLS on would otherwise hand freshly-joined
workers `http://` URLs they could not connect with.

### Domain-wide trust (via the DC)

For **domain-joined** Windows workers, push the CA once from the DC instead of
running `trust-lab-ca.ps1` on every box. Note the store: it must go into
**Trusted Root Certification Authorities** — *not* Trusted Publishers, which is
for code-signing and is ignored by TLS validation.

- **Group Policy (recommended):** Group Policy Management → a GPO linked to the
  workers' OU → Computer Configuration → Policies → Windows Settings → Security
  Settings → Public Key Policies → **Trusted Root Certification Authorities** →
  Import `ca.crt`. Members pick it up on the next `gpupdate /force` / reboot.
- **Enterprise store (autoenrollment):** on the DC,
  `certutil -dspublish -f ca.crt RootCA` — publishes the CA to AD; domain members
  auto-download it into their Root store.

Caveats: GPO/dspublish reach **domain-joined** machines only — standalone/workgroup
workers still need the manual `trust-lab-ca.ps1` import. Recent Git for Windows
defaults to `http.sslBackend=schannel` (uses the Windows Root store, so GPO trust
covers git too); if a worker's git uses the OpenSSL/mingw backend it consults its
own `ca-bundle.crt`, which GPO does not touch — set schannel or PEM-append there.

### The dashboard

The dashboard is served by `queue_server` itself at `/dashboard`, so it rides the
same port and cert — enable TLS and it is automatically at
`https://<HAL-ip>:5001/dashboard`. Its API calls are all relative
(`location.origin`), so they follow the scheme with no change. Over https the
copy-to-clipboard button also gains the native secure-context path.

Both servers share one cert/key pair. The CA key never leaves HAL. Only the
public `ca.crt` is distributed. All `*.key`/`*.crt`/`*.pem` are gitignored.

## Multi-host fleet (stretched LABNet)

Workers can run on **more than one physical host** while staying on a single
`192.168.100.0/24` L2 segment, so every worker reaches the control plane at one
address (`192.168.100.1`) — no routing or per-host IP ranges. This is done with a
point-to-point **VXLAN overlay** (VNI 100, UDP 4789) bridging LABNet across hosts.
Currently deployed across two KVM/libvirt hosts (HAL + Lenin). Full prerequisites,
per-host systemd units, join steps, and verification: [`docs/labnet-stretch.md`](docs/labnet-stretch.md).

## Scaling & known limits (honest take)

The queue/claim/lease/reaper/merge core is built for a small distributed fleet,
not a datacenter. We'd rather state the ceiling than pretend it isn't there.

### ~10 workers: feasible today, no redesign

- **SQLite single writer** is nowhere near saturated. Per-worker writes are a
  microsecond `claim` txn (`BEGIN IMMEDIATE`: one `SELECT pending` + one
  `UPDATE`), an idle-poll `upsert_worker` (~1/s), and a `/heartbeat` (60s while
  busy). 10 workers ≈ 10–15 tiny write-txns/sec vs WAL's tens-of-thousands/sec
  headroom. `busy_timeout=5000` is effectively never hit at this scale.
- **Werkzeug dev server** (`make_server(addr, port, app, threaded=True)`) parks
  ~12 threads (10 long-poll `/claim` + reaper + merge). Comfortable; it strains
  in the hundreds, not at 10.
- **`claim_next`** cap-matches in Python over `pending` rows — O(pending), not
  O(workers). Independent of fleet size.

### The real ceiling is merge throughput, not worker count

`merge_loop` is single-threaded and serialized. Historically each merge also ran
a fresh temp-worktree rebase **plus a full `HIVE_VERIFY_CMD` build/test**, so
`done` tasks queued behind one serialized build — the whole system's throughput
was `1 / (rebase + full_verify_build)` no matter how many workers ran.

**#10 — done (worker-side verify).** Workers now run the build/test gate in their
own clone, **in parallel**, and report the result plus the base sha they built on
(`verified` / `verified_base`, schema v7). The serial merge loop **trusts that
result only when the rebased tree it is about to fast-forward is byte-identical to
the tree the worker actually tested** (it compares the rebased worktree's tree to
the tree of the worker's reported commit). That is a provably-sound guard: if the
base drifted, or the agent's history reshaped the tree under rebase, the trees
differ and the merge loop **re-verifies** with the server-side gate. Fail-closed
safety is unchanged: unverified/failed trees never land, and a worker-side fail is
re-checked (not sticky) if a later rebase produces a different tree. Enable it by
setting `HIVE_VERIFY_CMD` in the **worker's** environment (same command contract
as the server gate); unset = old behaviour (merge loop verifies). Net effect:
verify cost moves off the single merge thread onto the N-wide worker pool, so
merge rate decouples from build time whenever the rebase is a clean no-op.

### To actually realize ~10 workers

- **#13 — done.** Workers `git clone --filter=blob:none` (partial clone) and the
  server sets `uploadpack.allowFilter`, so clone cost no longer scales with repo
  size per task over the LAN.
- **#10 — done.** Worker-side parallel verify + drift-guarded merge (above).

### Beyond ~25–30 workers (not there yet)

- **#11 — accepted limit (documented, deferred).** The control plane still uses
  the Werkzeug dev server with `reaper_loop`/`merge_loop` as **in-process**
  threads, and `claim_next` serializes on the one SQLite writer. This is fine to
  ~10–15 workers (see the write-rate math above). To scale past it, move serving
  to waitress/gunicorn **and** split reaper+merge into a single dedicated process
  — you must never run N copies of the merge loop (they'd race the fast-forward).
  Not worth the operational complexity until the fleet is well past current
  scale; the throughput bottleneck it addresses is downstream of #10, which is
  now fixed.
- **#12 — done.** Writers already carry a bounded `SQLITE_BUSY` retry
  (`with_retry` + `busy_timeout=5000`), so lock contention degrades gracefully
  rather than 500-ing `/claim`.
- At that scale, reconsider SQLite → Postgres for the queue.

Tracking: #23 (review roundup). Correctness core (atomic claim, lease/heartbeat/
reaper, fail-closed verify, dead-letter) is solid; these are throughput and
operability limits, called out on purpose.
