#!/usr/bin/env python3
"""hive_sessions.py -- single-pane status of all local Copilot CLI sessions.

Answers "which of my terminal sessions is active, working, or idle/waiting?"
without clicking through Konsole tabs.

Maps live `copilot` processes -> session-state dirs (via inuse.<pid>.lock),
reads each session's first user message as a label, and computes idle time from
events.jsonl mtime.

Usage:
  python3 hive_sessions.py            # one-shot table
  python3 hive_sessions.py --watch    # refresh every --interval seconds
  python3 hive_sessions.py --json     # machine-readable
"""
import argparse
import glob
import json
import os
import subprocess
import time

BASE = os.path.expanduser("~/.copilot/session-state")
IDLE_WAIT = 300  # seconds of quiet before a session is called "waiting"


def copilot_procs():
    out = subprocess.run(
        ["ps", "-eo", "pid,stat,etimes,args"], capture_output=True, text=True
    ).stdout.splitlines()
    procs = {}
    for line in out[1:]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, stat, etimes, args = parts
        if "copilot" in args and "grep" not in args and "hive_sessions" not in args:
            procs[pid] = {"stat": stat, "etimes": int(etimes), "args": args}
    return procs


def first_user_message(session_dir):
    ev = os.path.join(session_dir, "events.jsonl")
    if not os.path.exists(ev):
        return ""
    try:
        with open(ev) as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "user.message":
                    content = (obj.get("data", {}) or {}).get("content", "").strip()
                    if content:
                        return content.replace("\n", " ")
    except OSError:
        pass
    return ""


def fmt_dur(secs):
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def collect():
    procs = copilot_procs()
    rows = []
    seen_pids = set()
    for d in glob.glob(BASE + "/*/"):
        sid = os.path.basename(d.rstrip("/"))
        locks = glob.glob(d + "inuse.*.lock")
        live = []
        for lock in locks:
            try:
                pid = os.path.basename(lock).split(".")[1]
            except IndexError:
                continue
            if pid in procs:
                live.append(pid)
        if not live:
            continue
        ev = os.path.join(d, "events.jsonl")
        mtime = os.path.getmtime(ev) if os.path.exists(ev) else 0
        idle = int(time.time() - mtime) if mtime else -1
        label = first_user_message(d)[:64]
        for pid in live:
            if pid in seen_pids:
                continue  # ignore stale duplicate locks pointing at one live pid
            seen_pids.add(pid)
            stat = procs[pid]["stat"]
            if stat.startswith("R"):
                state = "ACTIVE"
            elif 0 <= idle < 120:
                state = "working"
            elif idle >= IDLE_WAIT:
                state = "WAITING"
            else:
                state = "idle"
            rows.append(
                {
                    "pid": pid,
                    "stat": stat,
                    "uptime_s": procs[pid]["etimes"],
                    "idle_s": idle,
                    "session": sid,
                    "state": state,
                    "label": label,
                }
            )
    rows.sort(key=lambda r: (r["state"] != "ACTIVE", r["idle_s"]))
    return rows


def render(rows):
    icons = {"ACTIVE": "\u25b6", "working": "\u2699", "WAITING": "\u23f8", "idle": "\u00b7"}
    print(f"{'':1} {'PID':>6} {'STATE':<8} {'UPTIME':>8} {'IDLE':>7}  {'SESSION':<9} TASK")
    for r in rows:
        icon = icons.get(r["state"], " ")
        idle = fmt_dur(r["idle_s"]) if r["idle_s"] >= 0 else "\u2014"
        print(
            f"{icon} {r['pid']:>6} {r['state']:<8} {fmt_dur(r['uptime_s']):>8} "
            f"{idle:>7}  {r['session'][:8]:<9} {r['label']}"
        )
    waiting = [r for r in rows if r["state"] == "WAITING"]
    if waiting:
        print(f"\n\u23f8  {len(waiting)} session(s) waiting for you: "
              + ", ".join(r["session"][:8] for r in waiting))
    print("\x1b[2m\u25b6 active  \u2699 working  \u23f8 waiting-for-you  \u00b7 idle\x1b[0m")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", action="store_true", help="refresh continuously")
    ap.add_argument("--interval", type=int, default=5, help="watch refresh seconds (default: %(default)s)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    if args.json:
        print(json.dumps(collect(), indent=2))
        return
    if not args.watch:
        render(collect())
        return
    try:
        while True:
            os.system("clear")
            print(f"Copilot sessions @ {time.strftime('%H:%M:%S')}  (refresh {args.interval}s, Ctrl-C to exit)\n")
            render(collect())
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
