#!/usr/bin/env python3
"""
HAL hive — shared capability vocabulary (single source of truth).

Both sides of the capability gate import THIS module so the strings always match:

  * workers  → probe_caps()      : what THIS box can actually do (advertised caps)
  * intake   → classify_prompt() : what a task's prompt APPEARS to need (requires)

The claim gate (hive_queue.claim) only lets a worker take a task when
`task.requires ⊆ worker.caps`, so the two functions below must speak the same
tag language. Keep every tag defined here and nowhere else.
"""

from __future__ import annotations

import os
import platform
import re
import shutil

# ---------------------------------------------------------------------------
# Canonical tag vocabulary. Adding a new capability = add it in BOTH the probe
# table and (if a prompt can imply it) the classifier table below.
# ---------------------------------------------------------------------------

# tool executable  ->  capability tag it grants when present on PATH
_TOOL_CAPS: dict[str, str] = {
    'dotnet':  'build:dotnet',
    'msbuild': 'build:msbuild',
    'go':      'build:go',
    'cargo':   'build:rust',
    'npm':     'build:node',
    'node':    'has:node',
    'python3': 'has:python',
    'python':  'has:python',
    'git':     'has:git',
    'docker':  'has:docker',
}

# any of these tags means the box can actually build something -> role:builder
_BUILD_TAGS = {'build:dotnet', 'build:msbuild', 'build:go',
               'build:rust', 'build:node'}


# ---------------------------------------------------------------------------
# Worker side: what can THIS host do?
# ---------------------------------------------------------------------------

def base_caps(lane: str | None = None) -> set[str]:
    """OS / arch / platform / lane-name tags — always advertised."""
    sysname = platform.system().lower()          # linux | windows | darwin
    caps = {f'os:{sysname}', f'arch:{platform.machine().lower()}'}
    caps.add('platform:windows' if sysname == 'windows' else 'platform:posix')
    if lane:
        caps.add(f'name:{lane.lower()}')
    return caps


def probe_caps(lane: str | None = None) -> set[str]:
    """Self-classify this box: base OS caps + every tool found on PATH, plus a
    coarse role:builder / role:tester verdict.

    Cheap and side-effect-free — uses shutil.which only (no subprocess), so it
    can run at every worker startup without risk of hanging."""
    caps = base_caps(lane)
    for exe, tag in _TOOL_CAPS.items():
        if shutil.which(exe):
            caps.add(tag)
    caps.add('role:builder' if (caps & _BUILD_TAGS) else 'role:tester')
    return caps


# ---------------------------------------------------------------------------
# Intake side: what does a task's prompt APPEAR to need?
# ---------------------------------------------------------------------------

# (compiled regex, tags-it-implies, human reason). First match wins per tag;
# all matching rules union their tags.
_RULES: list[tuple[re.Pattern, set[str], str]] = [
    (re.compile(r'\b(active\s*roles|\bars\b|oi\.ldap|edsva|adsi)\b', re.I),
     {'os:windows', 'build:dotnet'},
     'ActiveRoles/OI.Ldap builds only work on Windows (ar1) — SHA1 strong-name '
     '+ multi-address-connect break the Linux build'),
    (re.compile(r'\b(msbuild|\.sln\b|\.csproj\b|dotnet\s+(build|publish|restore|test)|nuget)\b', re.I),
     {'build:dotnet'},
     '.NET/MSBuild build'),
    (re.compile(r'\bon\s+windows\b|\bwindows[- ]only\b|\bwinrm\b|powershell', re.I),
     {'os:windows'},
     'explicitly Windows'),
    (re.compile(r'\bgo\s+(build|test|run)\b|\bgolang\b', re.I),
     {'build:go'}, 'Go build'),
    (re.compile(r'\bnpm\b|\byarn\b|\bnode\s+build\b|webpack|vite\b', re.I),
     {'build:node'}, 'Node/npm build'),
    (re.compile(r'\bcargo\s+(build|test)\b|\brust\b', re.I),
     {'build:rust'}, 'Rust build'),
    (re.compile(r'\bpytest\b|\bpython\b.*\btest', re.I),
     {'has:python'}, 'Python test run'),
    (re.compile(r'\bdocker\b|\bdockerfile\b|\bcompose\b', re.I),
     {'has:docker'}, 'Docker workload'),
]

# tokens that signal the prompt is too vague to classify confidently
_VAGUE = re.compile(r'\b(fix|investigate|look\s+into|the\s+flaky|something|'
                    r'the\s+thing|debug|why)\b', re.I)


# ---------------------------------------------------------------------------
# Complexity → model tiers.
#
# Complexity is chosen at task-creation time (required on `halctl add`) and maps
# to the model the worker runs copilot with: a cheap model for mechanical edits,
# a frontier model for architectural work. This is the single source of truth
# for the mapping — the add path stamps `model` onto the task from here, the
# worker just uses task['model'].
# ---------------------------------------------------------------------------

COMPLEXITY_MODELS: dict[int, str] = {
    1: 'claude-haiku-4.5',    # simple / mechanical
    2: 'claude-sonnet-4.6',   # standard coding
    3: 'claude-opus-4.8',     # complex / architectural
}
COMPLEXITY_LABELS: dict[int, str] = {1: 'simple', 2: 'standard', 3: 'complex'}


def _load_model_overrides() -> None:
    """F2 (#21): let operators remap complexity->model without editing code, so
    the hardcoded ids don't rot as models are renamed/retired. Precedence (low to
    high): built-in defaults, then ~/.hive/models.json (or $HIVE_MODELS_FILE),
    then the $HIVE_MODELS env var (JSON). Each source is a partial {level: id}
    map merged over the current mapping. Keys are 1..3 (int or str); bad/absent
    sources are ignored so a typo never takes the fleet down."""
    import json
    sources = []
    path = os.environ.get('HIVE_MODELS_FILE',
                          os.path.expanduser('~/.hive/models.json'))
    try:
        with open(path) as fh:
            sources.append(fh.read())
    except OSError:
        pass
    if os.environ.get('HIVE_MODELS'):
        sources.append(os.environ['HIVE_MODELS'])
    for raw in sources:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        for k, v in data.items():
            try:
                level = int(k)
            except (ValueError, TypeError):
                continue
            if level in (1, 2, 3) and isinstance(v, str) and v.strip():
                COMPLEXITY_MODELS[level] = v.strip()


_load_model_overrides()


def model_for_complexity(complexity: int) -> str:
    """Map a 1..3 complexity to its model id (defaults to the middle tier)."""
    return COMPLEXITY_MODELS.get(int(complexity), COMPLEXITY_MODELS[2])


# Architectural / high-reasoning signals -> tier 3 (opus).
_COMPLEX_RX = re.compile(
    r'\b(re-?architect|architecture|redesign|\bdesign\b|refactor|migrat\w+|'
    r'debug|investigat\w+|root\s*cause|diagnos\w+|race\s*condition|deadlock|'
    r'concurren\w+|thread[- ]?saf\w+|security|vulnerab\w+|\bcve\b|'
    r'optimi[sz]e|performance|end[- ]?to[- ]?end|\breview\b|'
    r'active\s*roles|oi\.ldap|across\s+\w+\s+(files|modules|services))\b', re.I)

# Mechanical / trivial signals -> tier 1 (haiku).
_SIMPLE_RX = re.compile(
    r'\b(create\s+a\s+file|add\s+a\s+(line|comment)|rename|fix\s+(a\s+)?typo|'
    r'bump\s+(the\s+)?version|update\s+the\s+version|reformat|whitespace|'
    r'delete\s+the|remove\s+the|one\s+line|readme|changelog|'
    r'\.gitignore|add\s+a\s+badge)\b', re.I)

_COMPLEX_LEN = 3000     # a very large prompt (e.g. an embedded PR diff) is complex
_SIMPLE_MAXLEN = 400    # a "simple" signal only counts on a short, focused prompt


def classify_complexity(prompt: str) -> dict:
    """Heuristically infer a task's complexity tier (1..3) and its model.

    Returns {complexity, model, label, reasons}. Precedence: any architectural
    signal (or a very large prompt) -> 3; else a mechanical signal on a short
    prompt -> 1; otherwise the standard middle tier -> 2. This backs the
    explicit `--complexity auto` opt-in; it is never applied silently (a bare
    `halctl add` requires the caller to pass a complexity).
    """
    text = prompt or ''
    reasons: list[str] = []
    complex_hit = bool(_COMPLEX_RX.search(text))
    if complex_hit:
        reasons.append('architectural/high-reasoning keyword')
    if len(text) > _COMPLEX_LEN:
        complex_hit = True
        reasons.append(f'large prompt ({len(text)} chars) — likely embedded spec/diff')

    if complex_hit:
        c = 3
    elif (_SIMPLE_RX.search(text) and len(text) < _SIMPLE_MAXLEN
          and len([ln for ln in text.splitlines() if ln.strip()]) <= 1):
        # Fail-upward: only a SINGLE-instruction, short, mechanically-worded
        # prompt earns the cheap tier. A multi-line prompt is several steps —
        # never silently downgrade that to haiku.
        c = 1
        reasons.append('short, single mechanical edit')
    else:
        c = 2
        if not reasons:
            reasons.append('no strong signal — default standard tier')
    return {'complexity': c, 'model': model_for_complexity(c),
            'label': COMPLEXITY_LABELS[c], 'reasons': reasons}


def classify_prompt(prompt: str) -> dict:
    """Infer required capability tags from a task prompt.

    Returns {requires, reasons, confidence}:
      confidence == 'high'   -> a concrete build/platform signal was found
      confidence == 'none'   -> nothing matched; safe to run anywhere
      confidence == 'ambiguous' -> vague verb with no concrete signal; ASK

    The intake skill uses confidence to decide whether to auto-add or ask the
    human first. The classifier NEVER guesses os:windows/build:* silently on an
    ambiguous prompt — that is the skill's job to confirm.
    """
    requires: set[str] = set()
    reasons: list[str] = []
    for rx, tags, why in _RULES:
        if rx.search(prompt):
            new = tags - requires
            if new:
                reasons.append(f'{why} -> {" ".join(sorted(new))}')
            requires |= tags

    if requires:
        confidence = 'high'
    elif _VAGUE.search(prompt):
        confidence = 'ambiguous'
    else:
        confidence = 'none'
    return {'requires': sorted(requires), 'reasons': reasons,
            'confidence': confidence}


if __name__ == '__main__':
    import json
    import sys
    if len(sys.argv) > 1:
        print(json.dumps(classify_prompt(' '.join(sys.argv[1:])), indent=2))
    else:
        print('this box advertises:', ' '.join(sorted(probe_caps('hal'))))
