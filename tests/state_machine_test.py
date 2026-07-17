#!/usr/bin/env python3
"""State-machine unit test for the HAL peer-review loop. Temp DB only.

Run: PYTHONPATH=~/hive python3 ~/hive/tests/state_machine_test.py

Two live workers (wA author, wB reviewer) are registered up front because the
single-worker graceful-degrade path (complete_task) skips review when no OTHER
online worker exists. The degrade path itself is asserted separately at the end
on a fresh DB with only one worker.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.expanduser('~/hive'))
import hive_queue as q

db = tempfile.mktemp(suffix='.db', prefix='hive-review-test-')
q.init_db(db)

AW = 'author-1'
AUTHOR_WORKER = 'wA'
OTHER_WORKER = 'wB'

# Register two live workers so a review is spawn-eligible (needs a non-author
# reviewer online). The real server upserts these on every /claim + /heartbeat.
q.upsert_worker(AUTHOR_WORKER, db_path=db)
q.upsert_worker(OTHER_WORKER, db_path=db)

def complete_success(tid, worker):
    return q.complete_task(tid, worker, exit_code=0, commit_hash='deadbeef'*5, db_path=db)

def row(tid):
    return q.get_task(tid, db_path=db)

q.add_task(AW, 'Implement widget X with tests.', review_max=2, db_path=db)
ct = q.claim_next(AUTHOR_WORKER, db_path=db)
assert ct.id == AW and ct.kind == 'work', ct
st = complete_success(AW, AUTHOR_WORKER)
assert st == 'awaiting_review', f'expected awaiting_review got {st}'
a = row(AW)
assert a['status'] == 'awaiting_review', a['status']
rev1 = f'{AW}-rev1'
r = row(rev1)
assert r is not None, 'review task not spawned'
assert r['kind'] == 'review', r['kind']
assert r['review_of'] == AW, r['review_of']
assert r['review_exclude_worker'] == AUTHOR_WORKER, r['review_exclude_worker']
assert 'Implement widget X with tests.' in r['prompt'], 'author prompt not embedded'
assert not r['requires'], f'review task should have no requires: {r["requires"]!r}'
print('PASS 1: success -> awaiting_review + spawned review task with embedded prompt')

got = q.claim_next(AUTHOR_WORKER, db_path=db)
assert got is None or got.id != rev1, f'author worker wrongly claimed review: {got}'
got2 = q.claim_next(OTHER_WORKER, db_path=db)
assert got2 is not None and got2.id == rev1, f'other worker failed to claim review: {got2}'
assert got2.kind == 'review' and got2.review_of == AW
print('PASS 2: claim anti-affinity (author excluded, other worker allowed)')

res = q.apply_review_verdict(AW, 'APPROVE', '', rev1, db_path=db)
assert res == 'approved', res
assert row(AW)['status'] == 'done', row(AW)['status']
assert row(rev1)['status'] == 'reviewed', row(rev1)['status']
assert row(AW)['review_round'] == 1
print('PASS 3: APPROVE -> author done, review task reviewed')

q2 = 'author-2'
q.add_task(q2, 'Build parser.', review_max=2, db_path=db)
q.claim_next(AUTHOR_WORKER, db_path=db)
complete_success(q2, AUTHOR_WORKER)
assert row(q2)['status'] == 'awaiting_review'
FIND1 = 'missing edge-case handling for empty input'
res = q.apply_review_verdict(q2, 'REQUEST_CHANGES', FIND1, f'{q2}-rev1', db_path=db)
assert res == 'rework', res
a2 = row(q2)
assert a2['status'] == 'pending', a2['status']
assert a2['review_round'] == 1, a2['review_round']
assert FIND1 in a2['prompt'], 'findings not appended to prompt'
assert 'Review feedback (round 1)' in a2['prompt']
print('PASS 4: REQUEST_CHANGES round1 -> pending, findings appended, round=1')

q.claim_next(AUTHOR_WORKER, db_path=db)
st = complete_success(q2, AUTHOR_WORKER)
assert st == 'awaiting_review', st
assert row(f'{q2}-rev2') is not None, 'second review task not spawned'
assert row(f'{q2}-rev2')['review_of'] == q2
print('PASS 4b: re-completion -> awaiting_review + new review task -rev2')

res = q.apply_review_verdict(q2, 'REQUEST_CHANGES', 'still broken', f'{q2}-rev2', db_path=db)
assert res == 'exhausted', res
a2 = row(q2)
assert a2['status'] == 'needs_attention', a2['status']
assert a2['review_round'] == 2
assert 'still broken' in (a2['error'] or '')
print('PASS 4c: REQUEST_CHANGES at cap -> needs_attention with findings in error')

q3 = 'author-3'
q.add_task(q3, 'Add cache layer.', review_max=1, db_path=db)
q.claim_next(AUTHOR_WORKER, db_path=db)
complete_success(q3, AUTHOR_WORKER)
res = q.apply_review_verdict(q3, 'lgtm ship it', 'garbled', f'{q3}-rev1', db_path=db)
assert res == 'exhausted', f'malformed verdict must not approve: {res}'
assert row(q3)['status'] == 'needs_attention', row(q3)['status']
print('PASS 5: malformed verdict treated as REQUEST_CHANGES (no auto-approve)')

res = q.apply_review_verdict(AW, 'APPROVE', '', rev1, db_path=db)
assert res == 'stale', res
print('PASS 6: stale/duplicate verdict guarded')

q4 = 'author-4'
q.add_task(q4, 'Task four.', review_max=1, db_path=db)
q.claim_next(AUTHOR_WORKER, db_path=db)
complete_success(q4, AUTHOR_WORKER)
conn = q.connect(db)
mergeable = conn.execute("SELECT id FROM tasks WHERE status='done'").fetchall()
conn.close()
assert q4 not in [m['id'] for m in mergeable], 'awaiting_review leaked into done set'
print('PASS 7: awaiting_review invisible to merge (status=done) selection')

q5 = 'author-5'
q.add_task(q5, 'Legacy task.', db_path=db)
q.claim_next(AUTHOR_WORKER, db_path=db)
st = complete_success(q5, AUTHOR_WORKER)
assert st == 'done', st
print('PASS 8: review_max=0 (default) -> straight to done (untouched behavior)')

# review kind completing via /complete -> reviewed (not done)
q6 = 'author-6'
q.add_task(q6, 'Task six.', review_max=1, db_path=db)
q.claim_next(AUTHOR_WORKER, db_path=db)
complete_success(q6, AUTHOR_WORKER)
rev = None
for _ in range(10):
    c = q.claim_next(OTHER_WORKER, db_path=db)
    if c is None:
        break
    if c.id == f'{q6}-rev1':
        rev = c
        break
assert rev is not None and rev.kind == 'review', rev
stc = q.complete_task(rev.id, OTHER_WORKER, exit_code=0, commit_hash='c'*20, db_path=db)
assert stc == 'reviewed', f'review /complete must yield reviewed: {stc}'
print('PASS 9: complete_task on kind=review -> reviewed (never done)')

# cancel author in awaiting_review cascades to its live review task
q7 = 'author-7'
q.add_task(q7, 'Task seven.', review_max=1, db_path=db)
q.claim_next(AUTHOR_WORKER, db_path=db)
complete_success(q7, AUTHOR_WORKER)
assert row(q7)['status'] == 'awaiting_review'
assert row(f'{q7}-rev1')['status'] == 'pending'
assert q.cancel_task(q7, db_path=db)
assert row(q7)['status'] == 'cancelled', row(q7)['status']
assert row(f'{q7}-rev1')['status'] == 'cancelled', row(f'{q7}-rev1')['status']
print('PASS 10: cancel author in awaiting_review cascades to review task')

# cancel a live review releases its author from awaiting_review -> needs_attention
q8 = 'author-8'
q.add_task(q8, 'Task eight.', review_max=1, db_path=db)
q.claim_next(AUTHOR_WORKER, db_path=db)
complete_success(q8, AUTHOR_WORKER)
assert row(q8)['status'] == 'awaiting_review'
assert q.cancel_task(f'{q8}-rev1', db_path=db)
assert row(f'{q8}-rev1')['status'] == 'cancelled'
assert row(q8)['status'] == 'needs_attention', row(q8)['status']
print('PASS 11: cancel review releases author -> needs_attention')

# requeue a needs_attention author resets review_round + cancels stray review
q9 = 'author-9'
q.add_task(q9, 'Task nine.', review_max=1, db_path=db)
q.claim_next(AUTHOR_WORKER, db_path=db)
complete_success(q9, AUTHOR_WORKER)
q.apply_review_verdict(q9, 'REQUEST_CHANGES', 'nope', f'{q9}-rev1', db_path=db)
assert row(q9)['status'] == 'needs_attention'
assert row(q9)['review_round'] == 1
assert q.requeue_task(q9, db_path=db)
assert row(q9)['status'] == 'pending', row(q9)['status']
assert row(q9)['review_round'] == 0, 'requeue must reset review_round'
print('PASS 12: requeue author resets review_round to 0')

os.remove(db)

# ── Single-worker graceful degrade (fresh DB, only ONE worker online) ──────────
db2 = tempfile.mktemp(suffix='.db', prefix='hive-degrade-test-')
q.init_db(db2)
q.upsert_worker('solo', db_path=db2)  # the ONLY worker
q.add_task('solo-1', 'Only worker in the fleet.', review_max=2, db_path=db2)
q.claim_next('solo', db_path=db2)
st = q.complete_task('solo-1', 'solo', exit_code=0, commit_hash='e'*20, db_path=db2)
assert st == 'done', f'single-worker fleet must skip review -> done, got {st}'
assert q.get_task('solo-1-rev1', db_path=db2) is None, 'no review task should spawn'
print('PASS 13: single-worker fleet -> review skipped, author straight to done')

os.remove(db2)

# ── Stalled-review reaper (reviewer went offline AFTER spawn) ──────────────────
db3 = tempfile.mktemp(suffix='.db', prefix='hive-stall-test-')
q.init_db(db3)
q.upsert_worker('wA', db_path=db3)
q.upsert_worker('wB', db_path=db3)
q.add_task('st-1', 'Stall me.', review_max=1, db_path=db3)
q.claim_next('wA', db_path=db3)
q.complete_task('st-1', 'wA', exit_code=0, commit_hash='f'*20, db_path=db3)
assert q.get_task('st-1', db_path=db3)['status'] == 'awaiting_review'
assert q.get_task('st-1-rev1', db_path=db3)['status'] == 'pending'
# Age the review past the stall threshold and take the reviewer offline.
conn = q.connect(db3)
conn.execute("UPDATE tasks SET created_at=datetime('now','-999 minutes') WHERE id='st-1-rev1'")
conn.execute("UPDATE workers SET last_seen=datetime('now','-999 minutes') WHERE worker_id='wB'")
conn.commit(); conn.close()
n = q.reap_stalled_reviews(db_path=db3)
assert n == 1, f'expected 1 degraded review, got {n}'
assert q.get_task('st-1-rev1', db_path=db3)['status'] == 'cancelled'
assert q.get_task('st-1', db_path=db3)['status'] == 'done', 'author must be promoted to done'
print('PASS 14: stalled unclaimable review -> reaper degrades, author -> done')

# A stalled review with a LIVE (busy) reviewer is left alone.
db4 = tempfile.mktemp(suffix='.db', prefix='hive-stall2-test-')
q.init_db(db4)
q.upsert_worker('wA', db_path=db4)
q.upsert_worker('wB', db_path=db4)
q.add_task('st-2', 'Stall me too.', review_max=1, db_path=db4)
q.claim_next('wA', db_path=db4)
q.complete_task('st-2', 'wA', exit_code=0, commit_hash='a'*20, db_path=db4)
conn = q.connect(db4)
conn.execute("UPDATE tasks SET created_at=datetime('now','-999 minutes') WHERE id='st-2-rev1'")
conn.commit(); conn.close()
n = q.reap_stalled_reviews(db_path=db4)
assert n == 0, f'live reviewer present -> no degrade, got {n}'
assert q.get_task('st-2', db_path=db4)['status'] == 'awaiting_review'
print('PASS 15: stalled review with a live reviewer is NOT degraded')

os.remove(db3); os.remove(db4)

# ── 16: live progress snapshot round-trips + is lease-guarded ──────────────────
import json as _json
dbp = tempfile.mktemp(suffix='.db', prefix='hive-progress-test-')
q.init_db(dbp)
q.upsert_worker('wP', db_path=dbp)
q.add_task('prog-1', 'Do a build.', db_path=dbp)
cp = q.claim_next('wP', db_path=dbp)
assert cp.id == 'prog-1'
# a fresh claim carries no progress yet
row_p = q.get_task('prog-1', db_path=dbp)
assert row_p['progress'] is None, 'fresh claim must have no progress'
snap = {'turn': 2, 'elapsed_s': 40, 'phase': 'working',
        'last_tool': {'tool': 'bash', 'detail': 'msbuild Service.sln', 'turn': '2'}}
ok = q.heartbeat('prog-1', 'wP', db_path=dbp, lease_token=cp.lease_token,
                 progress_json=_json.dumps(snap))
assert ok, 'owned heartbeat should renew'
got = q.get_task('prog-1', db_path=dbp)['progress']
assert got and got['turn'] == 2 and got['last_tool']['tool'] == 'bash', got
print('PASS 16a: progress snapshot written + parsed back on owned heartbeat')

# a stale-token beat is rejected AND must not overwrite the stored progress
ok2 = q.heartbeat('prog-1', 'wP', db_path=dbp, lease_token='WRONG',
                  progress_json=_json.dumps({'turn': 99}))
assert not ok2, 'stale-token beat must report not-owned'
still = q.get_task('prog-1', db_path=dbp)['progress']
assert still['turn'] == 2, f'stale beat must not overwrite progress, got {still}'
print('PASS 16b: stale-token beat neither renews nor overwrites progress')

# a beat with no progress must not clobber an existing snapshot (COALESCE)
ok3 = q.heartbeat('prog-1', 'wP', db_path=dbp, lease_token=cp.lease_token)
assert ok3
keep = q.get_task('prog-1', db_path=dbp)['progress']
assert keep and keep['turn'] == 2, 'progress-less beat must keep prior snapshot'
print('PASS 16c: progress-less heartbeat preserves prior snapshot (COALESCE)')

# re-claim clears stale progress
conn = q.connect(dbp)
conn.execute("UPDATE tasks SET status='pending', worker_id=NULL, lease_token=NULL, "
             "lease_expires=NULL WHERE id='prog-1'")
conn.commit(); conn.close()
cp2 = q.claim_next('wP', db_path=dbp)
assert cp2 and cp2.id == 'prog-1'
assert q.get_task('prog-1', db_path=dbp)['progress'] is None, 're-claim must reset progress'
print('PASS 16d: re-claim resets progress to NULL')
os.remove(dbp)

print('\nALL STATE-MACHINE ASSERTIONS PASSED')
