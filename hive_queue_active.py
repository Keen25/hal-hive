#!/usr/bin/env python3
"""hive_queue_active.py -- legible live view of ACTIONABLE hive tasks.

Top block = what the hive is doing NOW: one line per task whose status is
live (needs-attention / running / claimed / verifying / pending), each with a
plain-word label, colour, worker and branch. Terminal history (merged, probed,
reviewed, done, dead, cancelled) is collapsed into a single dim "lifetime"
footer so the pane stays readable. Refreshes in place.

Reads hive_queue.status_snapshot() directly instead of scraping halctl text.

Usage: python3 hive_queue_active.py [--interval N] [--once]
"""
import argparse
import os
import sys
import time

HIVE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HIVE_DIR)

# Live statuses, in the order we want them surfaced (most urgent first).
# status -> (glyph, plain label, ANSI colour code)
STYLE = {
    "needs_attention": ("⚠", "needs-attention", "1;31"),  # bold red
    "running":         ("▶", "running",         "32"),     # green
    "claimed":         ("◔", "claimed",         "36"),     # cyan
    "verifying":       ("⟳", "verifying",       "36"),     # cyan
    "awaiting_review": ("⧗", "awaiting-review", "35"),     # magenta
    "pending":         ("·", "pending",         "33"),     # yellow
}
ACTIVE_ORDER = list(STYLE.keys())
# Terminal / historical statuses collapsed into the footer.
LIFETIME_ORDER = ["merged", "reviewed", "probed", "done", "dead", "failed", "cancelled"]


def _c(code, s):
    return f"\x1b[{code}m{s}\x1b[0m"


def one_pass():
    import hive_queue as q
    try:
        snap = q.status_snapshot()
    except Exception as exc:  # noqa: BLE001 - keep the pane alive
        return [_c("31", f"status error: {exc}")]

    counts = snap.get("counts", {})
    tasks = snap.get("tasks", [])
    active = [t for t in tasks if t.get("status") in STYLE]

    out = []
    if active:
        active.sort(key=lambda t: (ACTIVE_ORDER.index(t["status"]),
                                   t.get("priority") if t.get("priority") is not None else 9))
        for t in active:
            glyph, label, colour = STYLE[t["status"]]
            wid = t.get("worker_id") or "—"
            branch = t.get("branch") or ""
            out.append(_c(colour, f"{glyph} {label:<15}") +
                       f" {t['id']:<16} {wid:<10} {branch}")
            if t.get("title"):
                out.append(_c("2", f"     ↳ {t['title']}"))
            if t["status"] == "needs_attention" and t.get("error"):
                out.append(_c("31", f"     ! {t['error']}"))
    else:
        out.append(_c("2", "  (nothing active right now)"))

    life = "  ".join(f"{s} {counts[s]}" for s in LIFETIME_ORDER if counts.get(s))
    if life:
        out.append("")
        out.append(_c("2", f"─ lifetime ─  {life}"))
    return out


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
            print(_c("1", "QUEUE · live") +
                  _c("2", f"    {time.strftime('%H:%M:%S')} · every {args.interval}s"))
            print(_c("2", "what the hive is doing right now") + "\n")
            print("\n".join(one_pass()), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
