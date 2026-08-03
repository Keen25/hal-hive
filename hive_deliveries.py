#!/usr/bin/env python3
"""hive_deliveries.py -- follow ALL hive delivery logs in the inbox dir.

Robust replacement for `tail -F inbox/*.jsonl`: works when no delivery files
exist yet and picks up new per-session <sid>.jsonl files as they appear. Prints
each delivery's summary line as it lands.

Usage: python3 hive_deliveries.py [--inbox-dir DIR] [--interval S] [--once]
"""
import argparse
import glob
import json
import os
import time

HIVE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INBOX = os.path.join(HIVE_DIR, "inbox")


def render(line):
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return line.rstrip()
    return rec.get("summary") or f"{rec.get('task_id')} [{rec.get('status')}]"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inbox-dir", default=DEFAULT_INBOX)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    os.makedirs(args.inbox_dir, exist_ok=True)
    offsets = {}

    def scan(emit):
        for path in sorted(glob.glob(os.path.join(args.inbox_dir, "*.jsonl"))):
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            # new file first seen: baseline files are already in offsets, so an
            # unknown path is one that appeared after startup -> read from 0
            start = offsets.get(path, 0)
            if size < start:  # truncated/rotated
                start = 0
            if emit and size > start:
                with open(path) as fh:
                    fh.seek(start)
                    for ln in fh:
                        if ln.strip():
                            print(f"\u2713 {render(ln)}", flush=True)
            offsets[path] = size

    # first scan establishes offsets at end-of-file (don't replay history)
    scan(emit=False)
    if args.once:
        return
    while True:
        try:
            scan(emit=True)
        except Exception as exc:  # keep pane alive
            print(f"[deliveries] {exc}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
