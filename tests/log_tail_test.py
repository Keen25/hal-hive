#!/usr/bin/env python3
"""Unit test for the distilled worker log tail (_read_log_tail). Temp files only.

Run: PYTHONPATH=~/hive python3 ~/hive/tests/log_tail_test.py

Regression target: a run's closing report was reliably LOST from the shipped
tail. The report is followed by a usage checkpoint carrying the full tool-schema
dump (~15 KB) plus a burst of background-task pings; on a 16 KB blind byte tail
those trailing ephemerals evicted the report every time, so build and test
numbers never reached HAL. Observed on a 2.9 MB log: 67 of 71 retained lines
were identical session.background_tasks_changed events.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.expanduser('~/hive'))
import worker_loop as w

REPORT = ('Build succeeded. 0 Error(s)\n'
          'Passed!  - Failed:     0, Passed:   123, Skipped:     0, Total:   123')


def build_log(path: str) -> None:
    """A realistic multi-MB copilot JSONL: bulk work, the closing report, then
    the ephemeral events that used to evict it."""
    with open(path, 'w', encoding='utf-8') as f:
        for i in range(4000):
            f.write(json.dumps({'type': 'tool.execution_start',
                                'data': {'toolName': 'bash', 'turnId': i,
                                         'arguments': {'command': 'x' * 600}}}) + '\n')
        f.write(json.dumps({'type': 'assistant.message', 'text': REPORT}) + '\n')
        # The eviction pair: one huge usage checkpoint + a ping burst.
        f.write(json.dumps({'type': 'session.usagecheckpoint',
                            'tools': [{'name': f't{i}', 'schema': 'z' * 200}
                                      for i in range(80)]}) + '\n')
        for _ in range(70):
            f.write(json.dumps({'type': 'session.background_tasks_changed',
                                'data': {'tasks': []}}) + '\n')
        f.write(json.dumps({'type': 'result', 'usage': {'total_tokens': 42}}) + '\n')


log = tempfile.mktemp(suffix='.jsonl', prefix='hive-logtail-test-')
build_log(log)
size = os.path.getsize(log)
assert size > 2_000_000, f'fixture too small to be representative: {size}'

tail = w._read_log_tail(log)
assert tail is not None, 'no tail produced'
assert len(tail) <= w.LOG_TAIL_BYTES * 1.2, f'tail blew the budget: {len(tail)}'
print(f'PASS 1: {size} byte log distilled to {len(tail)} chars')

assert 'Build succeeded' in tail, 'closing report evicted (the original defect)'
assert 'Passed!' in tail and 'Total:   123' in tail, 'test numbers missing'
print('PASS 2: closing report survives the trailing ephemeral events')

assert 'session.background_tasks_changed' not in tail, 'ping noise not filtered'
assert 'ephemeral events dropped' in tail, 'drop count not reported'
print('PASS 3: ephemeral noise dropped and accounted for')

assert '"type": "result"' in tail or '"type":"result"' in tail, 'result frame lost'
print('PASS 4: terminal result frame retained')

# The old blind byte tail is what the fix replaces: prove it actually failed.
with open(log, 'rb') as f:
    f.seek(-w.LOG_TAIL_BYTES, os.SEEK_END)
    naive = f.read().decode('utf-8', errors='replace')
assert 'Build succeeded' not in naive, 'fixture does not reproduce the defect'
print('PASS 5: fixture reproduces the defect under the old byte tail')

# A single event larger than the whole budget must still be clipped, not dropped.
big = tempfile.mktemp(suffix='.jsonl', prefix='hive-logtail-big-')
with open(big, 'w', encoding='utf-8') as f:
    f.write(json.dumps({'type': 'assistant.message', 'text': 'y' * 200_000}) + '\n')
bt = w._read_log_tail(big)
assert bt and len(bt) <= w.LOG_TAIL_BYTES * 1.2, f'oversized event not clipped: {len(bt or "")}'
print('PASS 6: single oversized event is clipped, not dropped')
os.remove(big)

# Non-JSONL logs must still fall back to a plain byte tail.
plain = tempfile.mktemp(suffix='.log', prefix='hive-logtail-plain-')
with open(plain, 'w', encoding='utf-8') as f:
    f.write('line-a\n' * 5000 + 'FINAL-LINE\n')
pt = w._read_log_tail(plain)
assert pt and 'FINAL-LINE' in pt, 'plain-text fallback lost the tail'
assert len(pt) <= w.LOG_TAIL_BYTES * 1.2, 'plain-text fallback blew the budget'
print('PASS 7: non-JSONL log falls back to a byte tail')
os.remove(plain)

assert w._read_log_tail(None) is None
assert w._read_log_tail('/nonexistent/path.jsonl') is None
print('PASS 8: missing path returns None')

os.remove(log)
print('\nALL LOG-TAIL ASSERTIONS PASSED')
