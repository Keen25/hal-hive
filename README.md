# HAL Hive — live task queue

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
| `worker_loop.py` | self-driving worker: claim → run `copilot` → push → complete; self-probes its caps |
| `halctl.py` | control CLI: `add` / `status` / `cancel` / `prio` / `classify` |
| `hive_caps.py` | capability vocabulary — `probe_caps()` (worker) + `classify_prompt()` (intake) |
| `gen-lab-ca.sh` | mint the lab CA + hive server cert (TLS, issue #8) |
| `trust-lab-ca.ps1` | trust the lab CA on a Windows worker (ar1) |
| `dispatch_dag.py` | legacy synchronous DAG dispatcher (kept, not the live path) |
| `winrm_run.py` / `winrm_push.py` | ar1 (Windows) WinRM helpers |
| `ad-overload/` | AD load-generator utility |
| `systemd/` | `--user` units: queue-server, `worker@`, git-daemon |
| `briefs/` | per-task spec inputs |
| `skills/` | Copilot CLI skills (`hive-queue-ops`, `hive-task-intake`) — backup of `~/.agents/skills/`; the live copies are what the agent loads |

## Run (systemd `--user`)

```bash
systemctl --user status  hive-queue-server hive-worker@hal hive-git-daemon
python3 halctl.py add "<prompt>" --require build:dotnet   # or use the hive-task-intake skill
python3 halctl.py status
```

## Secrets & state (NOT in this repo)

- Token + lab creds live in `~/.hive/` (`worker.token`, `hive.env` with
  `HIVE_TOKEN`, `LAB_PASS`). Scripts read `LAB_PASS` from the environment — source
  `~/.hive/hive.env` first, or they exit with a clear error.
- `queue.db`, `lab.git/`, and worker clones are runtime state (gitignored).

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

`merge_loop` is single-threaded and serialized, and each merge does a fresh
temp-worktree rebase **plus a full `HIVE_VERIFY_CMD` build/test**:

```
sustained merge rate ≈ 1 / (rebase + full_verify_build)
```

So beyond a point, adding workers hides copilot-run latency but does **not**
raise integration throughput — `done` tasks queue behind one serialized verify.
Nothing is lost (the queue stays correct); wall-clock parallelism is just capped
by the merge loop. If verify is a multi-minute .NET build, the merge loop is the
whole system's throughput. See #10.

### To actually realize ~10 workers

- **#13 — done.** Workers now `git clone --filter=blob:none` (partial clone) and
  the server sets `uploadpack.allowFilter`, so clone cost no longer scales with
  repo size per task over the LAN.
- **#10 — remaining.** Run verify on the worker (it already has the clone) and
  reserve the merge loop for the fast-forward. This decouples merge rate from
  build time and is the one change still needed to comfortably hit ~10.

### Beyond ~25–30 workers (not there yet)

- **#11** — move off the dev WSGI server (waitress/gunicorn) and split
  reaper+merge into a **single** dedicated process (never run N copies).
- **#12 — done.** Writers already carry a bounded `SQLITE_BUSY` retry
  (`with_retry` + `busy_timeout=5000`), so lock contention degrades gracefully
  rather than 500-ing `/claim`.
- At that scale, reconsider SQLite → Postgres for the queue.

Tracking: #23 (review roundup). Correctness core (atomic claim, lease/heartbeat/
reaper, fail-closed verify, dead-letter) is solid; these are throughput and
operability limits, called out on purpose.
