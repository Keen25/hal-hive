# TLS cutover — staged CA, and the fleet flip that follows

How to take the hive control plane (`:5001`) and git transport (`:8418`) from
plain HTTP to TLS, using the mini lab CA from `gen-lab-ca.sh` (issue #8 / S5).

The problem this closes: every request to `:5001` carries a **bearer token in a
header**, and the control token is effectively standard-user RCE on every worker
(see *Worker privilege model* in the README). On plain HTTP that token crosses
LABNet — and, via `192.168.1.85`, the house LAN — in clear text.

> **Status: staged, not cut over.** The CA and server cert exist and HAL trusts
> them, but `HIVE_TLS_CERT`/`HIVE_TLS_KEY` are **unset**, so both servers still
> speak plain HTTP and nothing has changed operationally. Step 0 below is done;
> steps 1–5 are not.

---

## The one hazard worth reading twice

`queue_server.py` and `git_http_server.py` are **env-gated, not dual-stack**.
Once both `HIVE_TLS_*` vars are set and the units restart, `:5001` and `:8418`
serve **https only**. There is no HTTP fallback and no redirect.

Every already-running worker is configured with `http://` URLs. At the moment
the servers restart, all of them stop connecting — simultaneously — and stay
down until each one both **trusts the CA** and is **re-pointed to https**. A
worker mid-task loses its lease, and the reaper eventually requeues the task.

Two consequences drive the ordering below:

- **Trust the CA on every worker *first*.** Installing a root cert while the
  servers are still HTTP changes nothing and breaks nothing — but it surfaces a
  failing import (typically the Windows box) while the fleet still works.
- **Cut over on an idle queue.** `halctl status` should show no in-flight tasks.

---

## Prerequisites

| Need | Why |
|------|-----|
| `openssl` on HAL | cert minting |
| `sudo` on HAL | installing the CA into the system trust store |
| SSH or WinRM to each online worker | trust + re-point |
| **Elevated** PowerShell on Windows workers | `LocalMachine\Root` import |
| Idle queue | a restart strands in-flight leases |

Inventory the fleet before starting — only **online** workers need the
coordinated flip; long-offline lanes get fixed whenever they next come back:

```bash
python3 halctl.py workers        # or: hive-status via the MCP server
```

---

## Step 0 — mint the CA and trust it on HAL *(done)*

```bash
./gen-lab-ca.sh                  # idempotent: reuses an existing CA
sudo cp ~/.hive-ca/ca.crt /etc/pki/ca-trust/source/anchors/hal-hive-ca.crt
sudo update-ca-trust
```

`gen-lab-ca.sh` auto-covers `127.0.0.1`, the lab NIC (`192.168.100.1`), every
non-loopback IPv4 on HAL, and the hostname. Pass extra SANs as arguments if a
worker dials HAL by a name or address not in that set — **a missing SAN is the
most common cause of a post-cutover connection failure**, and the cheapest thing
to get right before the flip.

Verify what you actually issued:

```bash
openssl verify -CAfile ~/.hive-ca/ca.crt ~/.hive-ca/server.crt
openssl x509 -in ~/.hive-ca/server.crt -noout -ext subjectAltName -dates
```

> The CA key never leaves HAL. `~/.hive-ca/` is outside this repo, and
> `.gitignore` additionally blocks `*.key`/`*.crt`/`*.pem`/`*.csr`/`*.srl` so a
> stray copy dropped in the tree cannot be committed.

### As deployed on HAL

| Item | Value |
|------|-------|
| CA dir | `~/.hive-ca/` (mode 700, keys 600) |
| Root CA | `CN=HAL Hive Lab CA`, RSA 4096, 10y |
| Server cert | `CN=HAL`, 825d |
| SANs | `DNS:HAL`, `DNS:localhost`, `IP:127.0.0.1`, `IP:192.168.100.1`, `IP:192.168.1.85`, `IP:192.168.1.69`, `IP:192.168.122.1` |
| HAL trust store | `/etc/pki/ca-trust/source/anchors/hal-hive-ca.crt` |

---

## Step 1 — pre-trust the CA on every online worker (non-disruptive)

Nothing below affects a running worker; it only prepares it. Do this while the
servers are still HTTP.

**Linux worker:**

```bash
scp ~/.hive-ca/ca.crt <worker>:/tmp/hal-hive-ca.crt
ssh <worker> 'sudo cp /tmp/hal-hive-ca.crt /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust'
```

**Windows worker (e.g. `ar1`):** copy `ca.crt` over, then from an **elevated**
PowerShell:

```powershell
.\trust-lab-ca.ps1 -CaPath C:\hive\ca.crt
```

That imports into `LocalMachine\Root` and sets git's `http.sslBackend=schannel`.
Note that **python-requests ignores the Windows store**, so the worker also needs
`REQUESTS_CA_BUNDLE` pointing at the PEM — `bootstrap.py` does this for freshly
joined boxes; an existing worker needs it added to its service environment.

Confirm trust took, *before* touching the servers:

```bash
# Linux worker — should print the cert, not a verify error
openssl s_client -connect 192.168.100.1:5001 -CAfile /etc/pki/tls/certs/ca-bundle.crt </dev/null
```

(While the server is still HTTP this will fail to handshake — that is expected.
The check that matters here is simply that `ca.crt` is present in the store.)

---

## Step 2 — enable TLS on the servers

Both server units already carry `EnvironmentFile=-%h/.hive/hive.env`, so the
cert path belongs there rather than in the unit files:

```bash
cat >> ~/.hive/hive.env <<'EOF'
HIVE_TLS_CERT=/home/afodor/.hive-ca/server.crt
HIVE_TLS_KEY=/home/afodor/.hive-ca/server.key
EOF
```

`~/.hive/hive.env` is shell-style `KEY=VALUE`; do not quote the paths, and do
not use `~` (systemd will not expand it).

---

## Step 3 — restart the servers *(this is the cut)*

```bash
systemctl --user restart hive-queue-server hive-git-http
systemctl --user status  hive-queue-server --no-pager
```

From here `:5001` and `:8418` are https-only and every un-migrated worker is
offline. Verify HAL itself immediately:

```bash
curl --cacert ~/.hive-ca/ca.crt https://127.0.0.1:5001/health     # expect 200
curl http://127.0.0.1:5001/health                                  # expect failure
```

---

## Step 4 — re-point the workers

A worker's control-plane URL comes from **`HIVE_QUEUE_URL`**, and its git remote
from `HIVE_LAB_GIT`/`HIVE_LAB_GIT_URL`. Change the scheme in the worker's own
environment, then restart it.

**HAL's local lane** (`hive-worker@hal`) — the unit hardcodes
`Environment=HIVE_QUEUE_URL=http://127.0.0.1:5001`, but systemd applies
`EnvironmentFile=` **after** all `Environment=` directives regardless of the
order they appear in the unit, so an entry in `hive.env` wins without editing
the unit (verified empirically, not assumed):

```bash
echo 'HIVE_QUEUE_URL=https://127.0.0.1:5001' >> ~/.hive/hive.env
systemctl --user restart hive-worker@hal
```

**Remote Linux worker:** set `HIVE_QUEUE_URL=https://192.168.100.1:5001` (and the
git URL to `https://192.168.100.1:8418/lab.git`) in that box's `~/.hive/hive.env`,
then restart its `hive-worker@<lane>` unit.

**Windows worker:** the equivalent values live in the WinSW service XML
(`<env name="HIVE_QUEUE_URL" .../>`, as emitted by `bootstrap.py`). Edit, then
restart the service.

Watch them come back:

```bash
python3 halctl.py workers        # each lane should return to online=True
```

---

## Step 5 — re-point the agent tooling

`hive_mcp.py` reads the same `HIVE_QUEUE_URL`, and Python's default SSL context
uses the system trust store — which already holds the lab CA after step 0. So on
HAL it needs only the scheme changed, in `~/.copilot/mcp-config.json`:

```jsonc
"hive": {
  "type": "local",
  "command": "/usr/bin/python3",
  "args": ["/home/afodor/hive/hive_mcp.py"],
  "env": { "HIVE_QUEUE_URL": "https://127.0.0.1:5001" },
  "tools": ["*"]
}
```

Restart the Copilot client (MCP servers are read at startup) and confirm
`hive-status` still answers. `halctl.py` picks the URL up from `hive.env`
automatically, so it needs no separate change.

---

## Newly-joined workers need nothing

`bootstrap.py::_tls_enabled()` derives the scheme from HAL's TLS state, so once
TLS is on, the dashboard's join one-liner emits `https://` URLs and plants the
CA on the new box by itself. Force the scheme with `HIVE_SCHEME=http|https` when
a reverse proxy terminates TLS instead. See *Newly-joined workers over TLS* in
the README.

---

## Rollback

TLS is env-gated, so reverting is unsetting two variables:

```bash
sed -i '/^HIVE_TLS_CERT=/d;/^HIVE_TLS_KEY=/d' ~/.hive/hive.env
systemctl --user restart hive-queue-server hive-git-http
```

Workers already flipped to `https://` will then fail until their
`HIVE_QUEUE_URL` goes back to `http://`. **Roll back the servers and the workers
together**, exactly as you cut over. Leaving the CA installed in a trust store is
harmless and saves repeating step 1.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `certificate verify failed: self signed certificate in chain` | CA not in that client's trust store | repeat step 1 on that box |
| `Hostname mismatch` / `IP address mismatch` | worker dials an address with no SAN | re-run `gen-lab-ca.sh <that-ip>`, redistribute nothing (CA unchanged), restart servers |
| Worker offline, log shows a connection reset | still on `http://` | step 4 for that lane |
| Windows worker trusts the cert but python still fails | requests ignores `LocalMachine\Root` | set `REQUESTS_CA_BUNDLE` to the PEM |
| `curl` works, git push fails on Windows | git not using schannel | `git config --global http.sslBackend schannel` (or set `GIT_SSL_CAINFO`) |
| Everything fails right after restart | cert/key path wrong or unreadable | `systemctl --user status hive-queue-server`; paths must be absolute, key mode 600 |
