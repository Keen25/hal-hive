#!/usr/bin/env python3
"""hive_pick.py -- interactive picker of resumable Copilot/HAL sessions.

Lists past sessions (from each session-state dir's workspace.yaml), newest
first, and lets you choose one to resume. The chosen session id is printed to
STDOUT (the menu + prompt go to STDERR/tty), so a caller can do:

    sid=$(python3 hive_pick.py) && copilot -r "$sid"

Keys at the prompt:
  <n>   resume session n        n     start a brand-new chat (prints nothing)
  q     quit (prints nothing)
"""
import glob
import os
import sys
import time

BASE = os.path.expanduser("~/.copilot/session-state")
LIMIT = 40

try:
    from hive_sessions import first_user_message
except Exception:  # pragma: no cover - standalone fallback
    def first_user_message(_d):
        return ""


def parse_workspace(path):
    """Minimal key: value parser (avoids a PyYAML dependency)."""
    data = {}
    try:
        with open(path) as fh:
            for line in fh:
                if ":" not in line or line.lstrip().startswith("#"):
                    continue
                k, _, v = line.partition(":")
                data[k.strip()] = v.strip()
    except OSError:
        pass
    return data


def is_live(session_dir):
    for lock in glob.glob(os.path.join(session_dir, "inuse.*.lock")):
        try:
            pid = int(os.path.basename(lock).split(".")[1])
        except (IndexError, ValueError):
            continue
        if os.path.exists(f"/proc/{pid}"):
            return True
    return False


def fmt_age(secs):
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def collect():
    rows = []
    for d in glob.glob(BASE + "/*/"):
        ws = parse_workspace(os.path.join(d, "workspace.yaml"))
        sid = ws.get("id") or os.path.basename(d.rstrip("/"))
        ev = os.path.join(d, "events.jsonl")
        mtime = os.path.getmtime(ev) if os.path.exists(ev) else 0
        if not mtime:
            continue  # never-used session dir
        named = ws.get("user_named", "false") == "true"
        name = ws.get("name", "")
        label = name if (named and name) else (first_user_message(d)[:56] or name)
        rows.append({
            "id": sid,
            "label": label or "(no messages)",
            "repo": ws.get("repository", "") or ws.get("cwd", ""),
            "mtime": mtime,
            "live": is_live(d),
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:LIMIT]


def main():
    rows = collect()
    if not rows:
        print("No resumable sessions found.", file=sys.stderr)
        return 0
    now = time.time()
    print("\033[1mResumable HAL sessions (newest first):\033[0m\n", file=sys.stderr)
    print(f"  {'#':>2}  {'AGE':>4}  {'':1} {'REPO':<22} TASK", file=sys.stderr)
    for i, r in enumerate(rows, 1):
        flag = "\033[32m*\033[0m" if r["live"] else " "
        repo = (r["repo"] or "")[-22:]
        print(f"  {i:>2}  {fmt_age(now - r['mtime']):>4}  {flag} {repo:<22} {r['label']}",
              file=sys.stderr)
    print("\n\033[2m* = currently live   ·   <#> resume   ·   n new chat   ·   q quit\033[0m",
          file=sys.stderr)

    try:
        choice = input("\nresume #> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return 0
    if choice in ("", "q", "quit"):
        return 0
    if choice == "n":
        return 10  # signal caller: start a fresh chat
    if choice.isdigit() and 1 <= int(choice) <= len(rows):
        print(rows[int(choice) - 1]["id"])  # STDOUT: the only machine-read line
        return 0
    print(f"Invalid choice: {choice}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
