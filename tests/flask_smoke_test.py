#!/usr/bin/env python3
"""Flask test_client smoke for POST /task/<id>/review auth + behavior."""
import os, sys, tempfile
sys.path.insert(0, os.path.expanduser('~/hive'))

db = tempfile.mktemp(suffix='.db', prefix='hive-flask-test-')
os.environ['HIVE_QUEUE_DB'] = db
os.environ['HIVE_TOKEN'] = 'CTL-TOKEN-123'
os.environ['HIVE_RO_TOKEN'] = 'RO-TOKEN-456'

import hive_queue as q
q.init_db(db)
import queue_server as srv
srv.q = q  # ensure same db module

# seed an author awaiting review + its review task
q.add_task('a1', 'do the thing', review_max=1, db_path=db)
q.upsert_worker('wA', db_path=db)
q.upsert_worker('wB', db_path=db)
q.claim_next('wA', db_path=db)
q.complete_task('a1', 'wA', exit_code=0, commit_hash='x'*20, db_path=db)
assert q.get_task('a1', db_path=db)['status'] == 'awaiting_review'

c = srv.app.test_client()
CTL = {'Authorization': 'Bearer CTL-TOKEN-123', 'Content-Type': 'application/json'}
RO = {'Authorization': 'Bearer RO-TOKEN-456', 'Content-Type': 'application/json'}
body = {'verdict': 'APPROVE', 'findings': '', 'review_task_id': 'a1-rev1'}

# no token -> 401
r = c.post('/task/a1/review', json=body)
assert r.status_code == 401, r.status_code
print('PASS: no token -> 401')

# read-only token -> 401 (control-gated, not in READONLY_PATHS)
r = c.post('/task/a1/review', json=body, headers=RO)
assert r.status_code == 401, r.status_code
print('PASS: read-only token -> 401')

# missing verdict -> 400
r = c.post('/task/a1/review', json={'review_task_id': 'a1-rev1'}, headers=CTL)
assert r.status_code == 400, r.status_code
print('PASS: missing verdict -> 400')

# missing review_task_id -> 400
r = c.post('/task/a1/review', json={'verdict': 'APPROVE'}, headers=CTL)
assert r.status_code == 400, r.status_code
print('PASS: missing review_task_id -> 400')

# control token, valid -> 200 approved
r = c.post('/task/a1/review', json=body, headers=CTL)
assert r.status_code == 200, r.status_code
j = r.get_json()
assert j['result'] == 'approved' and j['author'] == 'a1', j
assert q.get_task('a1', db_path=db)['status'] == 'done'
assert q.get_task('a1-rev1', db_path=db)['status'] == 'reviewed'
print('PASS: control token valid -> 200 approved, author done')

os.remove(db)
print('\nALL FLASK SMOKE ASSERTIONS PASSED')
