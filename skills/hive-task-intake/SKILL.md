---
name: hive-task-intake
description: >-
  Use when adding a coding task to the HAL live hive so it lands on a capable
  worker instead of failing on the wrong box. Runs the deterministic prompt
  classifier (halctl classify) to infer required capability tags
  (build:dotnet, os:windows, has:docker, ...), confirms or extends them, ASKS
  the human when the prompt is ambiguous, then calls halctl add with the right
  --require flags. Pairs with the worker self-probe (hive_caps.probe_caps) that
  makes each worker advertise its real toolset. USE FOR: "queue/add a hive
  task", "run this on the hive", "dispatch <coding task>". DO NOT USE FOR:
  inspecting/repairing a running queue (use hive-queue-ops), ARS product code,
  or ADO/Salesforce work.
---

# HAL Hive — Task Intake (auto-requirements)

Goal: never let a task land on a worker that can't do it — especially never let a
`.NET`/ARS build run on the Linux `hal` lane. The queue already enforces
`task.requires ⊆ worker.caps` at claim time; this skill makes sure every task is
**tagged** with the right `requires`, without the human hand-remembering tags.

Two cooperating pieces (both in `~/hive/`, single vocabulary in `hive_caps.py`):

| Piece | What it does |
|-------|--------------|
| `probe_caps()` (worker side) | each worker self-detects its toolset via `which()` and advertises `build:dotnet`, `has:docker`, `role:builder\|tester`, etc. Automatic — no `--cap` needed. |
| `classify_prompt()` (intake) | infers a task's `requires` from its prompt. Exposed as `halctl classify "<prompt>" [--json]`. |

## Procedure — run every time you add a task

1. **Classify the prompt first (never skip):**
   ```bash
   python3 ~/hive/halctl.py classify --json "<the task prompt>"
   ```
   Returns `{requires, reasons, confidence}`.

2. **Branch on `confidence`:**
   - `high` — concrete build/platform signal found. Add with the inferred
     `--require` tags (review `reasons`; add more `--require` only if you know
     something the classifier can't, e.g. a hidden native dep).
   - `none` — no signal; the task is portable. Add with **no** `build:*`/`os:*`
     require so any idle worker can take it.
   - `ambiguous` — vague verb (`fix the flaky thing`) with no concrete signal.
     **STOP and ask the human** what platform/toolset it needs (per the user's
     standing choice). Do NOT guess `os:windows`/`build:*` silently.

3. **Add with the confirmed tags:**
   ```bash
   python3 ~/hive/halctl.py add "<prompt>" \
       --require build:dotnet --require os:windows   # example
   ```
   Chain `--dep <task-id>` (dependency gate — enforced, task stays pending until
   the dep is `merged`) and `--worker <lane>` (hard pin to one box) as needed.
   `--require` = "any box that can do X"; `--worker` = "this exact box".

4. **Confirm placement:** `python3 ~/hive/halctl.py status` — a task whose
   `requires` no live worker satisfies stays `pending` and shows `requires=[...]`
   (never a silent stall). If it should run but no worker qualifies, either start
   a capable worker (see hive-queue-ops for the ar1/mini-HAL lane) or re-scope.

## Capability vocabulary (edit ONLY in `hive_caps.py`)

`os:{windows,linux,darwin}` · `platform:{windows,posix}` · `arch:*` ·
`build:{dotnet,msbuild,go,rust,node}` · `has:{git,python,node,docker}` ·
`role:{builder,tester}` · `name:<lane>` (lane pin).

## Sharp edges

- **`dotnet` on Linux is real.** The `hal` box has `dotnet` on PATH, so it
  advertises `build:dotnet` and *will* claim a bare `--require build:dotnet`
  task. That's fine for portable .NET, but **ARS/OI.Ldap builds must also carry
  `os:windows`** (the classifier's ARS rule adds it automatically) or they'll try
  to build on Fedora and fail (SHA1 strong-name + multi-address-connect).
- The classifier is **keyword-based**, deliberately conservative. It won't invent
  tags on an ambiguous prompt — that's why step 2 routes `ambiguous` to a human
  question rather than a guess.
- Worker caps are probed **at startup**. If you install a toolchain on a worker,
  restart it (`systemctl --user restart hive-worker@<lane>`) to re-advertise.
- Adding a new capability = add the tag in `hive_caps._TOOL_CAPS` (probe side)
  **and** `hive_caps._RULES` (classifier side). Same string on both sides or the
  gate never matches.
