#!/usr/bin/env python3
"""hive_queue_active.py -- compact live view of ACTIONABLE hive tasks.

Renders the `halctl status` summary line plus only the task blocks whose status
is actionable (claimed / pending / running / needs_attention). Merged, probed,
reviewed, dead and cancelled history is suppressed so the console pane stays
readable. Refreshes in place.

Usage: python3 hive_queue_active.py [--interval N] [--once]
"""
import argparse
import os
import subprocess
import time

HIVE_DIR = os.path.dirname(os.path.abspath(__file__))
KEEP = ("claimed", "pending", "running", "needs_attention", "needs attention")


def one_pass():
    try:
        out = subprocess.run(
            ["python3", "halctl.py", "status"],
            cwd=HIVE_DIR, capture_output=True, text=True, timeout=25,
        ).stdout.splitlines()
    except Exception as exc:  # noqa: BLE001 - keep pane alive
        return [f"status error: {exc}"]
    lines = []
    if out:
        lines.append(out[0])  # summary counts
    show = False
    for ln in out[1:]:
        head = ln.lstrip()
        # a task header line starts at column 0-ish with a status glyph, not a
        # continuation (↳ prompt / · meta / → arrow)
        is_header = bool(head) and head[:1] not in ("↳", "·", "→") and not ln.startswith("      ")
        if is_header:
            show = any(k in ln for k in KEEP)
        if show:
            lines.append(ln)
    if len(lines) <= 1:
        lines.append("(no active tasks)")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    if args.once:
        print("\n".join(one_pass()))
        return
    try:
        while True:
            print("\x1b[2J\x1b[H", end="")
            print(f"QUEUE (active)  {time.strftime('%H:%M:%S')}  refresh {args.interval}s\n")
            print("\n".join(one_pass()), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
