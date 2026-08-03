#!/usr/bin/env python3
"""hive_watch.py -- delivery->resume watcher.

Bridges the hive queue (queue.db) to a Copilot CLI session inbox so that when a
background worker finishes an offloaded task, the delivery surfaces back into the
interactive session ("pick up the thread").

Flow:
  1. You offload a task:  python3 halctl.py add "<defect...>" ...   -> prints task-XXXX
     then register it:     python3 hive_watch.py register task-XXXX
  2. The daemon polls queue.db; when a *registered* task reaches a terminal
     status (done_at set), it writes a delivery to the session inbox:
        - session.db  inbox_entries  (native CLI channel -> surfaces to the agent)
        - <inbox_dir>/<session>.jsonl (durable log, always written)
  3. Deduped via <inbox_dir>/state-<session>.json so each task delivers once.

Subcommands:
  run        Poll loop (default).            --interval N  --once  --all-since ISO
  register   Add task id(s) to the watchlist.
  unregister Remove task id(s) from the watchlist.
  list       Show current watchlist + delivered state.

Stdlib only. Safe to run detached.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

HIVE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_QUEUE_DB = os.path.join(HIVE_DIR, "queue.db")
DEFAULT_INBOX_DIR = os.path.join(HIVE_DIR, "inbox")

# Terminal statuses that count as a delivery. 'cancelled' is excluded by default
# (nothing to pick up); include with --include-cancelled.
TERMINAL_STATUSES = ("merged", "reviewed", "probed", "dead", "failed", "needs_attention")


def _env_session():
    """Best-effort autodetect of the current Copilot CLI session dir."""
    sid = os.environ.get("COPILOT_SESSION_ID")
    sdir = os.environ.get("COPILOT_SESSION_DIR")
    if sdir and not sid:
        sid = os.path.basename(sdir.rstrip("/"))
    if sid and not sdir:
        sdir = os.path.expanduser(f"~/.copilot/session-state/{sid}")
    return sid, sdir


def _paths(args):
    sid = args.session_id
    sdir = args.session_dir
    if not sid or not sdir:
        esid, esdir = _env_session()
        sid = sid or esid
        sdir = sdir or esdir
    session_db = args.session_db or (os.path.join(sdir, "session.db") if sdir else None)
    inbox_dir = args.inbox_dir
    os.makedirs(inbox_dir, exist_ok=True)
    return sid, session_db, inbox_dir


def _state_path(inbox_dir, sid):
    return os.path.join(inbox_dir, f"state-{sid}.json")


def _watch_path(inbox_dir, sid):
    return os.path.join(inbox_dir, f"watch-{sid}.json")


def _jsonl_path(inbox_dir, sid):
    return os.path.join(inbox_dir, f"{sid}.jsonl")


def _load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _now_epoch():
    return int(time.time())


def _connect_ro(db_path):
    # read-only, tolerate WAL from the live worker/queue writers
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_completed(conn, include_cancelled=False, all_since=None):
    statuses = list(TERMINAL_STATUSES)
    if include_cancelled:
        statuses.append("cancelled")
    placeholders = ",".join("?" for _ in statuses)
    sql = (
        "SELECT id, branch, repo, base_ref, worker_id, kind, priority, status, "
        "exit_code, verified, turns, cost_pr, commit_hash, done_at, log_path, "
        "log_tail, error, summary, prompt "
        f"FROM tasks WHERE status IN ({placeholders}) AND done_at IS NOT NULL"
    )
    params = list(statuses)
    if all_since:
        sql += " AND done_at >= ?"
        params.append(all_since)
    sql += " ORDER BY done_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def classify(row):
    status = row.get("status")
    if status in ("merged", "reviewed"):
        return "success"
    if status == "probed":
        return "success" if (row.get("exit_code") == 0) else "failure"
    if status in ("dead", "failed"):
        return "failure"
    if status == "needs_attention":
        return "attention"
    if status == "cancelled":
        return "cancelled"
    return "unknown"


def build_summary(row, outcome):
    tid = row.get("id")
    task_summary = (row.get("summary") or row.get("prompt") or "").strip().splitlines()
    head = task_summary[0][:80] if task_summary else "(no summary)"
    icon = {"success": "\u2705", "failure": "\u274c", "attention": "\u26a0\ufe0f"}.get(outcome, "\u2139\ufe0f")
    return f"{icon} hive {tid} [{row.get('status')}] {head}"


def build_content(row, outcome):
    def g(k):
        v = row.get(k)
        return "" if v is None else v
    tail = g("log_tail")
    if tail and len(str(tail)) > 4000:
        tail = str(tail)[-4000:]
    lines = [
        f"Hive task delivered: {g('id')}",
        f"Outcome: {outcome}  (status={g('status')}, exit_code={g('exit_code')}, verified={g('verified')})",
        f"Repo/branch: {g('repo')} @ {g('branch')} (base {g('base_ref')})",
        f"Worker: {g('worker_id')}   kind={g('kind')}  priority={g('priority')}  turns={g('turns')}  cost_pr={g('cost_pr')}",
        f"Commit: {g('commit_hash')}",
        f"Completed: {g('done_at')}",
    ]
    if g("error"):
        lines.append(f"Error: {g('error')}")
    if g("log_path"):
        lines.append(f"Log: {g('log_path')}")
    task_prompt = str(g("summary") or g("prompt") or "").strip()
    if task_prompt:
        lines.append("")
        lines.append("Original task:")
        lines.append(task_prompt[:1500])
    if tail:
        lines.append("")
        lines.append("Log tail:")
        lines.append(str(tail))
    return "\n".join(lines)


def write_inbox_entry(session_db, session_id, row, summary, content):
    """Best-effort insert into the native session inbox. Never fatal."""
    if not session_db or not session_id or not os.path.exists(session_db):
        return False, "no session.db"
    try:
        conn = sqlite3.connect(session_db, timeout=5)
        try:
            interaction_id = f"hive:{row['id']}"
            cur = conn.execute(
                "SELECT COALESCE(MAX(sequence), -1) FROM inbox_entries WHERE interaction_id=?",
                (interaction_id,),
            )
            seq = (cur.fetchone()[0] or -1) + 1
            entry_id = f"hive-{row['id']}-{seq}"
            conn.execute(
                "INSERT OR IGNORE INTO inbox_entries "
                "(id, recipient_session_id, sender_id, sender_name, sender_type, "
                " interaction_id, sequence, summary, content, unread, sent_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (
                    entry_id,
                    session_id,
                    "hive",
                    "hive-worker",
                    "agent",
                    interaction_id,
                    seq,
                    summary,
                    content,
                    _now_epoch(),
                ),
            )
            conn.commit()
            return True, entry_id
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, str(exc)


def deliver(row, sid, session_db, inbox_dir, verbose=True):
    outcome = classify(row)
    summary = build_summary(row, outcome)
    content = build_content(row, outcome)
    record = {
        "task_id": row["id"],
        "status": row.get("status"),
        "outcome": outcome,
        "repo": row.get("repo"),
        "branch": row.get("branch"),
        "commit_hash": row.get("commit_hash"),
        "exit_code": row.get("exit_code"),
        "verified": row.get("verified"),
        "worker_id": row.get("worker_id"),
        "done_at": row.get("done_at"),
        "summary": summary,
        "delivered_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(_jsonl_path(inbox_dir, sid), "a") as fh:
        fh.write(json.dumps(record) + "\n")
    ok, detail = write_inbox_entry(session_db, sid, row, summary, content)
    record["inbox_entry"] = detail if ok else f"skipped:{detail}"
    if verbose:
        print(f"[deliver] {summary}  -> inbox_entry={record['inbox_entry']}", flush=True)
    return record


def cmd_register(args):
    sid, _, inbox_dir = _paths(args)
    if not sid:
        sys.exit("cannot determine session id; pass --session-id")
    wp = _watch_path(inbox_dir, sid)
    watch = _load_json(wp, {})
    now = datetime.now(timezone.utc).isoformat()
    for tid in args.task_ids:
        watch[tid] = watch.get(tid, now)
    _save_json(wp, watch)
    print(f"watching {len(watch)} task(s) for session {sid}: {', '.join(sorted(watch))}")


def cmd_unregister(args):
    sid, _, inbox_dir = _paths(args)
    wp = _watch_path(inbox_dir, sid)
    watch = _load_json(wp, {})
    for tid in args.task_ids:
        watch.pop(tid, None)
    _save_json(wp, watch)
    print(f"watching {len(watch)} task(s) for session {sid}")


def cmd_wait(args):
    """Block until one/all watched (or given) task(s) complete, print delivery, exit.

    Designed to be launched as a detached background process: its EXIT is the
    push signal that wakes the interactive session.
    """
    sid, session_db, inbox_dir = _paths(args)
    if not sid:
        sys.exit("cannot determine session id; pass --session-id")
    targets = set(args.task_ids) if args.task_ids else set(_load_json(_watch_path(inbox_dir, sid), {}))
    if not targets:
        sys.exit("nothing to wait for: pass task ids or register some first")
    state_path = _state_path(inbox_dir, sid)
    state = _load_json(state_path, {"delivered": []})
    delivered = set(state.get("delivered", []))
    deadline = time.time() + args.timeout if args.timeout else None
    print(f"[wait] session={sid} targets={sorted(targets)} "
          f"mode={'all' if args.all else 'any'} timeout={args.timeout or '-'}", flush=True)
    while True:
        try:
            conn = _connect_ro(args.queue_db)
        except sqlite3.Error as exc:
            print(f"[warn] cannot open queue.db: {exc}", flush=True)
            time.sleep(args.interval)
            continue
        try:
            rows = {r["id"]: r for r in fetch_completed(conn, args.include_cancelled)}
        finally:
            conn.close()
        newly = [t for t in targets if t in rows and t not in delivered]
        for tid in newly:
            deliver(rows[tid], sid, session_db, inbox_dir, verbose=not args.quiet)
            delivered.add(tid)
        if newly:
            state["delivered"] = sorted(delivered)
            _save_json(state_path, state)
        done_targets = targets & delivered
        finished = (done_targets == targets) if args.all else bool(done_targets)
        if finished:
            print(f"[wait] DONE: delivered {sorted(done_targets)}", flush=True)
            return
        if deadline and time.time() >= deadline:
            print(f"[wait] TIMEOUT after {args.timeout}s; "
                  f"still pending {sorted(targets - delivered)}", flush=True)
            sys.exit(2)
        time.sleep(args.interval)


def cmd_list(args):
    sid, session_db, inbox_dir = _paths(args)
    watch = _load_json(_watch_path(inbox_dir, sid), {})
    state = _load_json(_state_path(inbox_dir, sid), {"delivered": []})
    print(f"session:   {sid}")
    print(f"session_db:{session_db}")
    print(f"inbox_dir: {inbox_dir}")
    print(f"watching:  {sorted(watch)}")
    print(f"delivered: {sorted(state.get('delivered', []))}")


def cmd_run(args):
    sid, session_db, inbox_dir = _paths(args)
    if not sid:
        sys.exit("cannot determine session id; pass --session-id")
    state_path = _state_path(inbox_dir, sid)
    state = _load_json(state_path, {"delivered": []})
    delivered = set(state.get("delivered", []))

    def one_pass():
        watch = _load_json(_watch_path(inbox_dir, sid), {})
        try:
            conn = _connect_ro(args.queue_db)
        except sqlite3.Error as exc:
            print(f"[warn] cannot open queue.db: {exc}", flush=True)
            return 0
        try:
            rows = fetch_completed(conn, args.include_cancelled, args.all_since)
        finally:
            conn.close()
        n = 0
        for row in rows:
            tid = row["id"]
            if tid in delivered:
                continue
            # deliver if explicitly watched, or --all-since mode watches everything
            if not args.all_since and tid not in watch:
                continue
            deliver(row, sid, session_db, inbox_dir, verbose=not args.quiet)
            delivered.add(tid)
            n += 1
        if n:
            state["delivered"] = sorted(delivered)
            _save_json(state_path, state)
        return n

    if args.once:
        count = one_pass()
        print(f"[once] delivered {count} new task(s)", flush=True)
        return
    print(
        f"[hive_watch] session={sid} queue={args.queue_db} interval={args.interval}s "
        f"all_since={args.all_since or '-'}",
        flush=True,
    )
    while True:
        try:
            one_pass()
        except Exception as exc:  # keep the daemon alive
            print(f"[error] pass failed: {exc}", flush=True)
        time.sleep(args.interval)


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--session-id", help="Copilot CLI session id (autodetected from env if omitted)")
    p.add_argument("--session-dir", help="session-state dir (contains session.db)")
    p.add_argument("--session-db", help="explicit path to session.db")
    p.add_argument("--queue-db", default=DEFAULT_QUEUE_DB, help="hive queue.db (default: %(default)s)")
    p.add_argument("--inbox-dir", default=DEFAULT_INBOX_DIR, help="delivery/state dir (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="poll loop (default)")
    r.add_argument("--interval", type=int, default=60, help="seconds between polls (default: %(default)s)")
    r.add_argument("--once", action="store_true", help="single pass then exit (for testing)")
    r.add_argument("--all-since", metavar="ISO", help="deliver EVERY task completing after this datetime, not just watched ones")
    r.add_argument("--include-cancelled", action="store_true", help="also deliver cancelled tasks")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(func=cmd_run)

    reg = sub.add_parser("register", help="add task id(s) to watchlist")
    reg.add_argument("task_ids", nargs="+")
    reg.set_defaults(func=cmd_register)

    unreg = sub.add_parser("unregister", help="remove task id(s) from watchlist")
    unreg.add_argument("task_ids", nargs="+")
    unreg.set_defaults(func=cmd_unregister)

    w = sub.add_parser("wait", help="block until watched task(s) complete, then exit (push signal)")
    w.add_argument("task_ids", nargs="*", help="task ids to wait for (default: current watchlist)")
    w.add_argument("--interval", type=int, default=30, help="seconds between polls (default: %(default)s)")
    w.add_argument("--timeout", type=int, default=0, help="max seconds to wait; 0 = forever")
    w.add_argument("--all", action="store_true", help="wait for ALL targets (default: return on first)")
    w.add_argument("--include-cancelled", action="store_true")
    w.add_argument("--quiet", action="store_true")
    w.set_defaults(func=cmd_wait)

    lst = sub.add_parser("list", help="show watchlist + delivered state")
    lst.set_defaults(func=cmd_list)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        # default to `run` with its defaults
        args = parser.parse_args((argv or []) + ["run"])
    args.func(args)


if __name__ == "__main__":
    main()
