#!/usr/bin/env bash
# hive-console.sh -- one-window operator console for Copilot sessions + the hive.
#
# Replaces juggling separate Konsole tabs. Four panes in one detachable tmux
# window (all read-only except the prompt you type):
#
#   ┌────────────────────────────┬───────────────────────────┐
#   │ SESSIONS (top-left)        │ QUEUE active (top-right)  │
#   │ hive_sessions.py --watch   │ hive_queue_active.py      │
#   ├────────────────────────────┼───────────────────────────┤
#   │ CLI (bottom-left)          │ DELIVERIES (bottom-right) │
#   │ interactive `copilot`      │ hive_deliveries.py        │
#   └────────────────────────────┴───────────────────────────┘
#
# Usage:
#   ./hive-console.sh            # attach (creates the session if needed)
#   ./hive-console.sh --kill     # tear the console down
#   HIVE_SESSION_ID=<sid> ./hive-console.sh   # bind delivery watcher to a session
#   HIVE_CLI_CMD / HIVE_CLI_DIR / HIVE_CLI_ARGS   # customize the CLI pane
#                                (e.g. HIVE_CLI_ARGS=--continue, HIVE_CLI_DIR=/repo)
#
# Detach with Ctrl-b d.  The tmux session survives detach/disconnect.
set -euo pipefail

HIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="hive-console"
PY="python3"

command -v tmux >/dev/null 2>&1 || { echo "tmux not found (dnf install tmux)." >&2; exit 1; }

if [[ "${1:-}" == "--kill" ]]; then
  tmux kill-session -t "$SESSION" 2>/dev/null && echo "killed $SESSION" || echo "no $SESSION session"
  exit 0
fi

SID="${HIVE_SESSION_ID:-}"

# ---- single-line pane commands (send-keys safe) ---------------------------
sessions_cmd="cd '$HIVE_DIR' && exec $PY hive_sessions.py --watch --interval 5"
queue_cmd="cd '$HIVE_DIR' && exec $PY hive_queue_active.py --interval 5"
if [[ -n "$SID" ]]; then
  deliver_cmd="cd '$HIVE_DIR' && mkdir -p inbox && echo 'DELIVERIES (session ${SID:0:8}) — offloaded tasks land here' && $PY hive_watch.py --session-id '$SID' run --interval 30 & exec $PY hive_deliveries.py"
else
  deliver_cmd="cd '$HIVE_DIR' && mkdir -p inbox && echo 'DELIVERIES — offloaded tasks appear here (bind with HIVE_SESSION_ID)' && exec $PY hive_deliveries.py"
fi
# CLI pane: launch an interactive Copilot CLI session so you can chat from
# inside the console. Configurable:
#   HIVE_CLI_CMD   command to run   (default: copilot; set to a shell to opt out)
#   HIVE_CLI_DIR   working dir      (default: \$HIVE_DIR)
#   HIVE_CLI_ARGS  extra CLI args   (e.g. "--continue" or "-C /path/to/repo")
CLI_BIN="${HIVE_CLI_CMD:-copilot}"
CLI_DIR="${HIVE_CLI_DIR:-$HIVE_DIR}"
CLI_ARGS="${HIVE_CLI_ARGS:-}"
if command -v "${CLI_BIN%% *}" >/dev/null 2>&1; then
  cli_cmd="cd '$CLI_DIR' && exec $CLI_BIN $CLI_ARGS"
else
  cli_cmd="cd '$CLI_DIR' && echo '[$CLI_BIN not found — falling back to shell. Set HIVE_CLI_CMD]' && exec \${SHELL:-/bin/bash}"
fi

# ---- build the window if absent (deterministic 2x2 layout) ----------------
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux new-session -d -s "$SESSION" -n console -x 220 -y 50
  W="$SESSION:console"

  # Start: single pane 0. Split horizontally -> 0 (left) | 1 (right).
  tmux split-window -h -t "$W.0"
  # Split left pane vertically  -> 0 (top-left) / 2 (bottom-left).
  tmux split-window -v -t "$W.0"
  # Split right pane vertically -> 1 (top-right) / 3 (bottom-right).
  tmux split-window -v -t "$W.1"
  tmux select-layout -t "$W" tiled

  # Pane map after the splits above:
  #   0 top-left    -> sessions
  #   2 bottom-left -> CLI (interactive Copilot)
  #   1 top-right   -> queue
  #   3 bottom-right-> deliveries
  tmux send-keys -t "$W.0" "$sessions_cmd" C-m
  tmux send-keys -t "$W.2" "$cli_cmd"      C-m
  tmux send-keys -t "$W.1" "$queue_cmd"    C-m
  tmux send-keys -t "$W.3" "$deliver_cmd"  C-m

  tmux select-pane -t "$W.2"   # focus the CLI
fi

if [[ -n "${TMUX:-}" ]]; then
  tmux switch-client -t "$SESSION"
else
  exec tmux attach -t "$SESSION"
fi
