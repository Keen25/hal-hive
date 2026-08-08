#!/usr/bin/env python3
"""
halctl — HAL control-plane CLI for the live swarm (Phase 9.1)

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
    'verifying': '⟳', 'awaiting_review': '⧗', 'reviewed': '⊚',
    'merged': '●', 'needs_attention': '⚠', 'failed': '✗',
    'dead': '☠', 'cancelled': '⊘', 'probed': '⊙',
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
               deps=a.dep, max_attempts=a.max_attempts, summary=a.summary,
               review_max=a.review, repo=a.repo,
               disable_mcp=a.disable_mcp,
               kind='probe' if getattr(a, 'probe', False) else 'work')
    bits = [f'+ {tid}', f'prio={a.priority}',
            f'cx={complexity}/{model}{auto_note}',
            f'branch={a.branch or f"feature/{tid}"}']
    if getattr(a, 'probe', False):
        bits.append('kind=probe')
    if a.repo and a.repo != 'lab.git':
        bits.append(f'repo={a.repo}')
    if a.worker:
        bits.append(f'pin={a.worker}')
    if a.require:
        bits.append(f'requires={",".join(a.require)}')
    if a.dep:
        bits.append(f'deps={",".join(a.dep)}')
    if getattr(a, 'disable_mcp', None):
        bits.append(f'disable_mcp={",".join(q.parse_mcp_names(a.disable_mcp))}')
    if a.review:
        bits.append(f'review={a.review}')
    print('  '.join(bits))
    eff_summary = ' '.join(a.summary.split())[:q.SUMMARY_MAX] if a.summary \
        else q._task_title(a.prompt, q.SUMMARY_MAX)
    print(f'  ↳ {eff_summary}')
    if not getattr(a, 'no_wire_hint', False):
        import os
        sid = os.environ.get('COPILOT_SESSION_ID') or '<sid>'
        sdir = os.environ.get('COPILOT_SESSION_DIR') \
            or f'$HOME/.copilot/session-state/{sid}'
        print('  ↻ report-back NOT wired — dispatch is half the job. To be woken '
              'on completion, register + launch a TRACKED async watcher (never '
              'setsid/detached):')
        print(f'      python3 ~/hive/hive_watch.py --session-id {sid} '
              f'--session-dir {sdir} register {tid}')
        print(f'      python3 ~/hive/hive_watch.py --session-id {sid} '
              f'--session-dir {sdir} wait {tid} --interval 15 --timeout 900')
        if sid == '<sid>':
            print('    (env COPILOT_SESSION_ID empty — substitute this session\'s '
                  'id. skip with --no-wire-hint for fire-and-forget.)')


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


# ── promote: the human-gated bridge from the hive mirror to ADO ────────────────
# review -> validate -> apply. Air-gap preserved: the hive never pushes to ADO on
# its own; an operator runs `halctl promote <task>` and confirms before anything
# reaches the ADO PR branch.

_ADO_RESOURCE = '499b84ac-1321-427f-aa17-267ca6975798'  # Azure DevOps AAD resource
_ADO_ARS_URL = ('https://dev.azure.com/1id/Active%20Roles/_git/ActiveRoles')


def _run(cmd, cwd=None, check=True, capture=True):
    import subprocess
    r = subprocess.run(cmd, cwd=cwd, text=True,
                       capture_output=capture)
    if check and r.returncode != 0:
        err = (r.stderr or r.stdout or '').strip()
        raise RuntimeError(f'command failed ({r.returncode}): {" ".join(cmd)}\n{err}')
    return r


def _ado_token():
    r = _run(['az', 'account', 'get-access-token', '--resource', _ADO_RESOURCE,
              '--query', 'accessToken', '-o', 'tsv'])
    tok = r.stdout.strip()
    if not tok:
        raise RuntimeError('could not mint ADO token (az account get-access-token '
                           'returned empty — run `az login`)')
    return tok


def cmd_promote(a):
    """Promote a delivered mirror task's branch onto its ADO PR branch.

    Flow: fetch the task branch from the local hive mirror -> show the diff and
    commit list (REVIEW) -> summarize what will change (VALIDATE) -> on --yes,
    cherry-pick the task's commits onto the live ADO PR branch and push (APPLY).
    Default is a dry run: nothing is pushed without --yes.
    """
    import os
    import subprocess
    import sqlite3
    import tempfile
    import shutil

    # 1. Look up the task.
    con = q.connect(q.QUEUE_DB)
    con.row_factory = sqlite3.Row
    row = con.execute('SELECT id, branch, base_ref, repo, status FROM tasks WHERE id=?',
                      (a.task_id,)).fetchone()
    if row is None:
        print(f'unknown task {a.task_id}', file=sys.stderr); sys.exit(2)
    repo = row['repo'] or 'lab.git'
    if repo == 'lab.git':
        print(f'{a.task_id} is a lab.git task — promote is for mirror-repo (ADO) '
              f'tasks only.', file=sys.stderr); sys.exit(2)
    branch = row['branch']
    base_ref = row['base_ref']
    pr_branch = a.pr_branch or base_ref
    mirror_dir = os.path.expanduser(a.mirror_dir or os.path.join(q.HIVE_DIR, repo))
    if row['status'] not in ('merged', 'reviewed', 'done'):
        print(f'warning: {a.task_id} status is {row["status"]} (expected merged/'
              f'reviewed) — the worker may not have delivered its branch yet.',
              file=sys.stderr)

    if not os.path.isdir(mirror_dir):
        print(f'mirror repo not found: {mirror_dir}', file=sys.stderr); sys.exit(2)

    SB = ['-c', 'safe.bareRepository=all']
    # Confirm the task branch exists on the mirror.
    r = _run(['git', *SB, '--git-dir', mirror_dir, 'rev-parse', '--verify',
              f'refs/heads/{branch}'], check=False)
    if r.returncode != 0:
        print(f'task branch {branch} not present on mirror {repo} — did the worker '
              f'push it? (status={row["status"]})', file=sys.stderr); sys.exit(2)

    print(f'promote {a.task_id}: mirror {repo} branch {branch} -> ADO {pr_branch}')

    # 2. Fresh ADO clone at the PR branch (no persisted auth header -> avoids the
    #    duplicate-Authorization 400). Blobless for speed; we only need the tip
    #    tree + the ability to cherry-pick.
    tok = _ado_token()
    authhdr = f'http.extraheader=Authorization: Bearer {tok}'
    work = tempfile.mkdtemp(prefix=f'promote-{a.task_id}-')
    keep_work = False
    try:
        cl = _run(['git', '-c', authhdr, 'clone', '--filter=blob:none',
                   '--single-branch', '--branch', pr_branch,
                   _ADO_ARS_URL, work], check=False)
        if cl.returncode != 0:
            print(f'ADO clone of {pr_branch} failed:\n{cl.stderr.strip()}',
                  file=sys.stderr); sys.exit(1)

        def g(*args, check=True):
            return _run(['git', '-c', authhdr, *args], cwd=work, check=check)

        g('config', 'user.name', 'Arpad Fodor')
        g('config', 'user.email', 'Arpad.Fodor@quest.com')

        # 3. Bring the task branch's commits in from the hive mirror.
        g('remote', 'add', 'hive', mirror_dir)
        g('-c', 'safe.bareRepository=all', 'fetch', 'hive', branch)
        # The task's own commits = base_ref..task_branch. base_ref exists in the
        # ADO clone (it's the PR branch we cloned). Resolve the mirror base too.
        mirror_base = _run(['git', *SB, '--git-dir', mirror_dir, 'rev-parse',
                            f'refs/heads/{base_ref}'], check=False).stdout.strip()
        rangespec = f'{mirror_base}..FETCH_HEAD' if mirror_base else 'FETCH_HEAD~1..FETCH_HEAD'
        commits = g('rev-list', '--reverse', rangespec).stdout.split()

        # 4. REVIEW — show the diff and commit list.
        print(f'\n── commits to promote ({len(commits)}) ─────────────────────────')
        print(g('log', '--oneline', '--no-decorate', rangespec, '-n', '50').stdout.rstrip())
        print('\n── diffstat ─────────────────────────────────────────────────')
        print((g('diff', '--stat', mirror_base, 'FETCH_HEAD').stdout.rstrip()
               if mirror_base else g('show', '--stat', 'FETCH_HEAD').stdout.rstrip()))
        if a.show_diff:
            print('\n── full diff ────────────────────────────────────────────────')
            print(g('diff', mirror_base, 'FETCH_HEAD').stdout)

        # 5. VALIDATE — summary + guardrails.
        print('\n── validate ─────────────────────────────────────────────────')
        print(f'  target ADO branch : {pr_branch}')
        print(f'  commits to apply  : {len(commits)}')
        print(f'  strategy          : cherry-pick onto live {pr_branch} tip, then push')
        print('  note              : ADO CI is the authoritative build/test gate')

        if not commits:
            print('\nnothing to promote (no commits in range).'); return

        # 6. APPLY — only with --yes.
        if not a.yes:
            print('\n[dry run] re-run with --yes to cherry-pick and push to ADO.')
            print('          add --show-diff to print the full patch.')
            return

        cp = g('cherry-pick', *commits, check=False)
        if cp.returncode != 0:
            print(f'cherry-pick failed (conflict) — resolve manually in {work}:\n'
                  f'{cp.stdout}\n{cp.stderr}', file=sys.stderr)
            print(f'workspace kept at {work} for manual resolution.')
            keep_work = True
            return  # keep workspace; do not rmtree in finally
        push = g('push', 'origin', f'HEAD:{pr_branch}', check=False)
        if push.returncode != 0:
            print(f'push to ADO failed:\n{push.stderr.strip()}', file=sys.stderr)
            sys.exit(1)
        tip = g('rev-parse', 'HEAD').stdout.strip()
        print(f'\n✓ promoted {a.task_id}: {len(commits)} commit(s) pushed to '
              f'ADO {pr_branch} (tip {tip[:10]}).')
        print('  ADO CI will now validate. Reply/resolve the PR review thread if applicable.')
    finally:
        if os.path.isdir(work) and not keep_work:
            shutil.rmtree(work, ignore_errors=True)


def cmd_status(a):
    snap = q.status_snapshot()
    counts = snap['counts']
    order = ['running', 'claimed', 'pending', 'done', 'verifying',
             'awaiting_review', 'reviewed', 'merged', 'probed',
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


def cmd_worker_rm(a):
    if q.remove_worker(a.worker_id):
        print('removed', a.worker_id)
    else:
        print('no such worker', a.worker_id)


def cmd_prune_workers(a):
    removed = q.prune_workers(a.stale_after)
    if removed:
        for wid in removed:
            print('removed', wid)
    else:
        print('no stale workers')


def cmd_update(a):
    import hive_version
    version = a.version if a.version is not None else hive_version.CODE_VERSION
    if a.all:
        n = q.set_worker_update(version, worker_id=None)
        print(f'update -> v{version} requested for ALL workers ({n} row(s))')
    else:
        n = q.set_worker_update(version, worker_id=a.worker_id)
        if n:
            print(f'update -> v{version} requested for {a.worker_id}')
        else:
            print('no such worker', a.worker_id)


def cmd_workers(a):
    """Quirks view: each worker's host, install dir, os:/build: caps, and code
    version at a glance — the fast way to spot a mis-placed or stale worker."""
    import hive_version
    workers = q.list_workers()
    if not workers:
        print('(no workers registered)')
        return
    srv = hive_version.CODE_VERSION
    print(f'server code version: v{srv}')
    print('─' * 78)
    for w in workers:
        online = w.get('online')
        dot = '●' if online and w['status'] != 'stale' else ('○' if online else '✗')
        cv = w.get('code_version')
        ver = f'v{cv}' if cv is not None else 'v?'
        stale = (cv is not None and cv != srv)
        upd = w.get('update_to')
        # Surface just the os:/build:/role: tags — the "quirks" that matter.
        tags = [t for t in (w.get('caps') or '').split()
                if t.split(':', 1)[0] in ('os', 'build', 'has', 'role', 'arch')]
        print(f"{dot} {w['worker_id']:9s} {ver:5s}"
              f"{'  ⚠update' if stale else '        '} "
              f"{(w.get('hostname') or '?'):14s} {w.get('host') or '?'}")
        print(f"    install: {w.get('install_dir') or '?'}")
        if tags:
            print(f"    caps:    {' '.join(sorted(tags))}")
        meta = []
        if upd is not None:
            meta.append(f'update_to=v{upd}')
        meta.append(f"status={w['status']}")
        meta.append(f"age={w.get('age_s')}s")
        if w.get('python_exe'):
            meta.append(f"py={w['python_exe']}")
        print(f"    · {'  '.join(meta)}")


def cmd_requeue(a):
    ok = q.requeue_task(a.task_id, reset_attempts=not a.keep_attempts)
    if ok:
        note = '' if a.keep_attempts else ' (attempts reset)'
        print(f'requeued -> pending{note}', a.task_id)
    else:
        print('not requeueable (must be needs_attention|dead|cancelled|awaiting_review)', a.task_id)


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
    pa.add_argument('--review', type=int, default=0, metavar='N',
                    help='spawn N rounds of automated peer review before merge '
                         '(0 = disabled, default)')
    pa.add_argument('--repo', default='lab.git',
                    help='central bare repo the task clones/pushes, served on '
                         ':8418 (default lab.git). Use ActiveRoles.git for ARS '
                         'source tasks so the change lands centrally and is '
                         'reviewable + promotable to ADO via `halctl promote`.')
    pa.add_argument('--disable-mcp', action='append', metavar='SERVER',
                    help='MCP server name to suppress for this task\'s copilot '
                         'run (repeatable; also accepts a comma/space list). '
                         'Names must match the key in ~/.copilot/mcp-config.json '
                         '(e.g. salesforce, pagerduty). Scopes the agent toolset '
                         'per session via --disable-mcp-server without touching '
                         'the global MCP config. Default: load all configured.')
    pa.add_argument('--probe', action='store_true',
                    help='read-only / diagnostic task (kind=probe): a run that '
                         'produces no commit finishes terminally (probed) on the '
                         'first attempt instead of requeuing. Never merges. Use '
                         'for build checks, log reads, validation runs.')
    pa.add_argument('--no-wire-hint', action='store_true',
                    help='suppress the report-back watcher-wiring reminder printed '
                         'after queueing (use for fire-and-forget / scripted adds '
                         'you will not report on)')
    pa.set_defaults(fn=cmd_add)

    ps = sub.add_parser('status', help='fleet snapshot')
    ps.set_defaults(fn=cmd_status)

    pcl = sub.add_parser('classify',
                         help='infer required capability tags from a prompt (no add)')
    pcl.add_argument('prompt')
    pcl.add_argument('--json', action='store_true', help='machine-readable output')
    pcl.set_defaults(fn=cmd_classify)

    ppr = sub.add_parser('promote',
                         help='review+push a delivered mirror task branch to its ADO PR branch')
    ppr.add_argument('task_id')
    ppr.add_argument('--pr-branch', help='ADO target branch (default: the task base_ref)')
    ppr.add_argument('--mirror-dir', help='hive bare mirror path (default ~/hive/<repo>)')
    ppr.add_argument('--show-diff', action='store_true', help='print the full patch, not just diffstat')
    ppr.add_argument('--yes', action='store_true',
                     help='APPLY: cherry-pick onto the ADO PR branch and push '
                          '(default is a dry-run review only)')
    ppr.set_defaults(fn=cmd_promote)

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

    pw = sub.add_parser('worker', help='worker registry commands')
    wsub = pw.add_subparsers(dest='worker_cmd', required=True)
    pwrm = wsub.add_parser('rm', help='remove a worker from the registry')
    pwrm.add_argument('worker_id')
    pwrm.set_defaults(fn=cmd_worker_rm)

    pws = sub.add_parser('workers',
                         help='quirks view: host, install dir, os/build caps, '
                              'code version per worker')
    pws.set_defaults(fn=cmd_workers)

    ppw = sub.add_parser('prune-workers',
                         help='remove all stale/offline workers from the registry')
    ppw.add_argument('--stale-after', type=int, default=q.WORKER_STALE_S,
                     dest='stale_after',
                     help=f'age threshold in seconds (default: {q.WORKER_STALE_S})')
    ppw.set_defaults(fn=cmd_prune_workers)

    import hive_version
    pu = sub.add_parser('update',
                        help='ask worker(s) to self-update to the current code '
                             f'version (v{hive_version.CODE_VERSION})')
    gu = pu.add_mutually_exclusive_group(required=True)
    gu.add_argument('--worker', dest='worker_id',
                    help='one worker_id (lane) to update')
    gu.add_argument('--all', action='store_true',
                    help='update every registered worker')
    pu.add_argument('--version', type=int,
                    help='target CODE_VERSION (default: current server version)')
    pu.set_defaults(fn=cmd_update)

    args = p.parse_args()
    args.fn(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
