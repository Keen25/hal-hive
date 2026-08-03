#!/usr/bin/env bash
# hive-console.sh -- one-window operator console for Copilot sessions + the hive.
#
# Replaces juggling separate Konsole tabs. The CLI you type in is DOMINANT on the
# left; three read-only glance panes stack in a narrow right column.
#
#   ┌────────────────────────────┬───────────────────────────┐
#   │                            │ SESSIONS (top-right)      │
#   │  CLI (left, ~68%)          │ hive_sessions.py --watch  │
#   │  interactive `copilot`     ├───────────────────────────┤
#   │  — type here               │ QUEUE (mid-right)         │
#   │                            │ hive_queue_active.py      │
#   │                            ├───────────────────────────┤
#   │                            │ DELIVERIES (bottom-right) │
#   │                            │ hive_deliveries.py        │
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
  # No fixed -x/-y: inherit the attaching client's REAL size. The old
  # `-x 220 -y 50` capped the console at 220x50 cells even on a 4K display,
  # which is why the CLI pane felt tiny.
  tmux new-session -d -s "$SESSION" -n console
  W="$SESSION:console"

  # Resize to the most-recent client on attach, and label every pane border so
  # it is obvious "what is what".
  tmux set-option        -t "$SESSION" window-size latest
  tmux set-window-option -t "$SESSION" aggressive-resize on
  tmux set-option        -t "$SESSION" pane-border-status top
  tmux set-option        -t "$SESSION" pane-border-format ' #{pane_title} '

  # Layout: the CLI (where you actually type) is DOMINANT on the left (~68%);
  # the three glance-only watchers stack in a narrow right column (~32%).
  #   ┌────────────────────┬──────────────┐
  #   │                    │ SESSIONS     │  pane 1
  #   │  CLI  (pane 0)     ├──────────────┤
  #   │  interactive       │ QUEUE        │  pane 2
  #   │  copilot           ├──────────────┤
  #   │                    │ DELIVERIES   │  pane 3
  #   └────────────────────┴──────────────┘
  tmux split-window -h -l 32% -t "$W.0"     # 0 left (CLI) | 1 right column
  tmux split-window -v -l 66% -t "$W.1"     # 1 top / 2 (lower two-thirds)
  tmux split-window -v -l 50% -t "$W.2"     # 2 mid / 3 bottom -> ~equal thirds

  tmux select-pane -t "$W.0" -T 'CLI · copilot (type here)'
  tmux select-pane -t "$W.1" -T 'SESSIONS · your copilot windows'
  tmux select-pane -t "$W.2" -T 'QUEUE · hive tasks'
  tmux select-pane -t "$W.3" -T 'DELIVERIES · finished offloads'

  tmux send-keys -t "$W.0" "$cli_cmd"      C-m
  tmux send-keys -t "$W.1" "$sessions_cmd" C-m
  tmux send-keys -t "$W.2" "$queue_cmd"    C-m
  tmux send-keys -t "$W.3" "$deliver_cmd"  C-m

  tmux select-pane -t "$W.0"   # focus the CLI
fi

if [[ -n "${TMUX:-}" ]]; then
  tmux switch-client -t "$SESSION"
else
  exec tmux attach -t "$SESSION"
fi
