#!/usr/bin/env python3
"""
halctl — HAL control-plane CLI for the live hive (Phase 9.1)

Talks to queue.db directly (HAL-local; HAL is the single writer/clock source).
This is what you use from HAL's interactive session while workers run in the
background:

    halctl add   "add binary_search.py" [--id task-031] [--priority 1]
                 [--dep task-030 ...] [--worker ar1] [--branch feature/x]
    halctl status
    halctl cancel task-035
    halctl prio  task-034 1
    halctl init

None of these block: workers are already running and pull from the queue.
"""

from __future__ import annotations

import argparse
import sys
import uuid

import hive_queue as q

_ICON = {
    'running': '▶', 'claimed': '◔', 'pending': '·', 'done': '✓',
    'verifying': '⟳', 'merged': '●', 'needs_attention': '⚠', 'failed': '✗',
    'dead': '☠', 'cancelled': '⊘',
}


def cmd_add(a):
    import hive_caps
    tid = a.id or f'task-{uuid.uuid4().hex[:8]}'
    if a.complexity == 'auto':
        cl = hive_caps.classify_complexity(a.prompt)
        complexity, model = cl['complexity'], cl['model']
        auto_note = f" (auto: {cl['label']} — {'; '.join(cl['reasons'])})"
    else:
        complexity = int(a.complexity)
        model = hive_caps.model_for_complexity(complexity)
        auto_note = ''
    q.add_task(tid, a.prompt, branch=a.branch, base_ref=a.base_ref,
               worker=a.worker, requires=a.require, scope=a.scope,
               priority=a.priority, complexity=complexity, model=model,
               deps=a.dep, max_attempts=a.max_attempts, summary=a.summary)
    bits = [f'+ {tid}', f'prio={a.priority}',
            f'cx={complexity}/{model}{auto_note}',
            f'branch={a.branch or f"feature/{tid}"}']
    if a.worker:
        bits.append(f'pin={a.worker}')
    if a.require:
        bits.append(f'requires={",".join(a.require)}')
    if a.dep:
        bits.append(f'deps={",".join(a.dep)}')
    print('  '.join(bits))
    eff_summary = ' '.join(a.summary.split())[:q.SUMMARY_MAX] if a.summary \
        else q._task_title(a.prompt, q.SUMMARY_MAX)
    print(f'  ↳ {eff_summary}')


def cmd_classify(a):
    import json
    import hive_caps
    r = hive_caps.classify_prompt(a.prompt)
    cx = hive_caps.classify_complexity(a.prompt)
    if a.json:
        print(json.dumps({**r, 'complexity': cx['complexity'],
                          'model': cx['model'], 'complexity_label': cx['label'],
                          'complexity_reasons': cx['reasons']}))
        return
    print(f"confidence: {r['confidence']}")
    print(f"requires:   {' '.join(r['requires']) or '(none — runs anywhere)'}")
    for why in r['reasons']:
        print(f"  · {why}")
    print(f"complexity: {cx['complexity']} ({cx['label']}) -> {cx['model']}")
    for why in cx['reasons']:
        print(f"  · {why}")


def cmd_status(a):
    snap = q.status_snapshot()
    counts = snap['counts']
    order = ['running', 'claimed', 'pending', 'done', 'verifying', 'merged',
             'needs_attention', 'dead', 'cancelled']
    summary = '  '.join(f'{_ICON.get(s,"?")} {s}:{counts[s]}'
                        for s in order if s in counts)
    print(summary or '(queue empty)')
    print('─' * 72)
    for t in snap['tasks']:
        line = (f"{_ICON.get(t['status'],'?')} {t['id']:16s} "
                f"{t['status']:15s} p{t['priority']} "
                f"{t['worker_id'] or '':10s} {t['branch']}")
        print(line)
        if t.get('title'):
            print(f"      ↳ {t['title']}")
        constraints = []
        if t.get('worker'):
            constraints.append(f"pin={t['worker']}")
        if t.get('requires'):
            constraints.append(f"requires=[{t['requires']}]")
        if constraints:
            print(f'      · {"  ".join(constraints)}')
        if t['status'] == 'needs_attention' and t['error']:
            print(f'      ↳ {t["error"]}')


def cmd_cancel(a):
    print('cancelled' if q.cancel_task(a.task_id) else 'not cancellable', a.task_id)


def cmd_requeue(a):
    ok = q.requeue_task(a.task_id, reset_attempts=not a.keep_attempts)
    if ok:
        note = '' if a.keep_attempts else ' (attempts reset)'
        print(f'requeued -> pending{note}', a.task_id)
    else:
        print('not requeueable (must be needs_attention|dead|cancelled)', a.task_id)


def cmd_prio(a):
    print('ok' if q.reprioritize(a.task_id, a.priority) else 'no such task',
          a.task_id, '->', a.priority)


def cmd_init(a):
    q.init_db()
    q.ensure_lab_repo()
    print(f'queue.db ready (schema v{q.SCHEMA_VERSION}) at {q.QUEUE_DB}')


def main():
    p = argparse.ArgumentParser(prog='halctl')
    sub = p.add_subparsers(dest='cmd', required=True)

    pa = sub.add_parser('add', help='queue a task')
    pa.add_argument('prompt')
    pa.add_argument('--id')
    pa.add_argument('--branch')
    pa.add_argument('--base-ref', default=q.DEFAULT_BASE_REF)
    pa.add_argument('--worker', help='pin to one exact lane (ar1|hal|...)')
    pa.add_argument('--require', action='append',
                    help='capability a worker MUST have to claim this (repeatable), '
                         'e.g. --require os:windows --require msbuild')
    pa.add_argument('--scope', help='build/test scope for the verify gate')
    pa.add_argument('--summary',
                    help='short (<=128 char) human label shown on the dashboard/CLI '
                         'instead of the full prompt (auto-derived from the prompt '
                         'when omitted)')
    pa.add_argument('--priority', type=int, default=5)
    pa.add_argument('--complexity', default='auto',
                    choices=['1', '2', '3', 'auto'],
                    help='model tier: 1=haiku(simple) 2=sonnet(standard) '
                         '3=opus(complex), or "auto" to classify from the prompt '
                         '(default: auto)')
    pa.add_argument('--dep', action='append', help='dependency task id (repeatable)')
    pa.add_argument('--max-attempts', type=int, default=3)
    pa.set_defaults(fn=cmd_add)

    ps = sub.add_parser('status', help='fleet snapshot')
    ps.set_defaults(fn=cmd_status)

    pcl = sub.add_parser('classify',
                         help='infer required capability tags from a prompt (no add)')
    pcl.add_argument('prompt')
    pcl.add_argument('--json', action='store_true', help='machine-readable output')
    pcl.set_defaults(fn=cmd_classify)

    pc = sub.add_parser('cancel', help='cancel a task')
    pc.add_argument('task_id')
    pc.set_defaults(fn=cmd_cancel)

    prq = sub.add_parser('requeue',
                         help='re-enqueue a needs_attention|dead|cancelled task')
    prq.add_argument('task_id')
    prq.add_argument('--keep-attempts', action='store_true',
                     help='do NOT reset the attempt counter (default: reset to 0)')
    prq.set_defaults(fn=cmd_requeue)

    pp = sub.add_parser('prio', help='reprioritize a pending task')
    pp.add_argument('task_id')
    pp.add_argument('priority', type=int)
    pp.set_defaults(fn=cmd_prio)

    pi = sub.add_parser('init', help='initialise queue.db')
    pi.set_defaults(fn=cmd_init)

    args = p.parse_args()
    args.fn(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
