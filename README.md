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
| `dispatch_dag.py` | legacy synchronous DAG dispatcher (kept, not the live path) |
| `winrm_run.py` / `winrm_push.py` | ar1 (Windows) WinRM helpers |
| `ad-overload/` | AD load-generator utility |
| `systemd/` | `--user` units: queue-server, `worker@`, git-daemon |
| `briefs/` | per-task spec inputs |

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
