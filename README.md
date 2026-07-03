# HAL Hive — live task queue

Pull-persistent SQLite task queue that dispatches Copilot CLI coding tasks to
distributed workers (mini-HALs) and merges results into `lab.git`. HAL's session
is a live control plane; nothing blocks while the fleet runs.

Design history + phase docs: gist `Keen25/08d90e38b9a9aad47120ef8e8b38036b`.

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
