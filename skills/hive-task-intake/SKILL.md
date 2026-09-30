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

> **⛔ Mental model — a task is a QUEUE ADD, never a remote build.** You are
> writing a prompt for a worker that will **pull** the task, run `copilot`
> locally, and push its own branch. You are NOT SSH/WinRM-ing into a box to run
> a build. If you catch yourself planning "connect to ar1 and build," you've
> slipped into the dead pre-Phase-9 push model — the whole job here is to
> `halctl add` with the right `--require`/`--worker` so the *right box pulls it*.
> (Transport details: see hive-queue-ops "Transport model".)

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
   Returns `{requires, reasons, confidence, complexity, model,
   complexity_label, complexity_reasons}` — capability tags AND the suggested
   model tier (1=haiku/simple, 2=sonnet/standard, 3=opus/complex).

2. **Branch on `confidence`:**
   - `high` — concrete build/platform signal found. Add with the inferred
     `--require` tags (review `reasons`; add more `--require` only if you know
     something the classifier can't, e.g. a hidden native dep).
   - `none` — no **build/OS** signal. This means *portable ONLY IF the task is
     stateless*. **`none` is NOT a synonym for "run anywhere"** — the classifier
     is keyword-based and **blind to locality** (see 2a). Run the pin check
     before you treat a `none` task as portable.
   - `ambiguous` — vague verb (`fix the flaky thing`) with no concrete signal.
     **STOP and ask the human** what platform/toolset it needs (per the user's
     standing choice). Do NOT guess `os:windows`/`build:*` silently.

2a. **Locality / state check — the classifier CANNOT see this; you must (never
   skip on a `none`/`high` task before adding):** a task is bound to one exact
   box — pin it with `--worker <lane>`, do **not** leave it portable — when the
   prompt shows any of:

   | Signal in the prompt | Why portable is wrong | Route to |
   |----------------------|-----------------------|----------|
   | "you are running on host X", "on ar1/wg1/sql1", a specific IP (`192.168.…`) | Task is written *for that box* | `--worker <lane>` |
   | reuse existing credentials / token / oauth / login already on the box | Creds don't exist on other lanes | `--worker <lane>` |
   | a tool/binary "already installed here", "the box that has X" | Only that lane has it (probe-invisible if off-PATH) | `--worker <lane>` |
   | NDJ / workgroup / domain-joined / DC-specific | Identity is host-specific | `--worker <lane>` |
   | must reach a private host/service only one box can see | Network reachability isn't a cap | `--worker <lane>` |

   `--require` = "any box that can do X" (a *capability*). `--worker` = "this
   exact box" (*locality/state*). The classifier only ever emits capabilities;
   **locality is 100% your call.** Real miss: a probe that had to reuse the
   `hal` box's Copilot oauth classified `none` — trusting that verbatim would
   have landed it on a Windows lane with no creds. Every `deploy-*`/NDJ task in
   the queue carries **empty** `requires` and works *only* because it was
   hand-pinned. When unsure whether a `none` task is truly portable, **ask the
   human** rather than scatter it.

3. **Add with the confirmed tags AND a complexity (required):**
   ```bash
   python3 ~/hive/halctl.py add "<prompt>" --complexity 2 \
       --require build:dotnet --require os:windows   # example
   ```
   `--complexity` is **mandatory** (`1|2|3|auto`) — it picks the copilot model
   the worker runs (cheap for mechanical work, opus for architectural). Use the
   classifier's suggested tier, pass `auto` to let it decide explicitly, or
   override with your judgement. Chain `--dep <task-id>` (dependency gate —
   enforced, task stays pending until the dep is `merged`) and `--worker <lane>`
   (hard pin) as needed. `--require` = "any box that can do X"; `--worker` =
   "this exact box".

4. **Confirm placement — and that the tags can actually be satisfied:**
   `python3 ~/hive/halctl.py status` (and `halctl.py workers` for live caps). A
   task whose `requires` no live worker satisfies stays `pending` forever with
   `requires=[...]` — never a silent stall, but it *will* sit until you cancel
   it. Before you walk away, verify **both**:
   - **Every `--require` tag is real vocabulary AND a live worker advertises
     it.** A tag no worker carries (e.g. the phantom `app:sql` — not in the
     vocabulary below) can *never* match: the claim gate enforces
     `requires ⊆ worker.caps` **even for pinned tasks**, so a `--worker` pin
     does NOT rescue a bad `--require`. `halctl classify` only emits known tags;
     any tag you add by hand must be in *Capability vocabulary* below and
     currently probed by a live lane.
   - **Any `--worker <lane>` pin points at an ONLINE lane.** Pinning
     `nonexistent-lane` (or an offline box) = permanent `pending` → eventual
     cancel. Check the lane shows in `halctl.py workers` first.

   If it should run but no worker qualifies, either start a capable worker (run
   the **`/bootstrap.ps1`|`/bootstrap.sh` one-liner** from the dashboard's "Add a
   worker" panel on a box with the needed toolset — see hive-queue-ops
   "Provisioning a new worker") or re-scope.

5. **Wire report-back (never skip if you're expected to report):** register
   the task to this session AND launch a **tracked async `hive_watch.py wait`**
   in the same turn. See *Report-back wiring* below — the "daemon alone is
   enough, no shell needed" claim that used to live here was **verified false**
   on 2026-08-25 and must not come back.

## Report-back wiring — close the loop (never skip after add)

Dispatching is only half the job. **The only thing in this whole stack that can
actually wake a live session is a tracked async shell exiting** (bash
`mode=async`, not `setsid`/detached — its completion fires the runtime's own
notification). Verified 2026-08-25: `grep -a "inbox_entries"` /
`"recipient_session_id"` against the actual `copilot` binary returns **zero
matches** — the CLI has no code path that reads `inbox_entries`, marks it read,
or surfaces it mid-turn. There is no passive "inbox poller." A row can sit
`unread=1` forever and nothing will ever tap you on the shoulder about it.

So: `hive-notifier.service` (`hive_watch.py notify`) is real and useful, but
**only as a durable audit log / crash-recovery safety net** — it writes a
delivery row + a `inbox/<sid>.jsonl` line for every terminal task on a watched
session, decoupled from any one shell's lifetime. It does NOT push anything to
you. Treat it the same way you'd treat a log file: something to *check*, never
something that *notifies*.

**Primary mechanism (use every time):**

1. **Register** the task to this session's watchlist (also records the
   session's `session.db` path in `inbox/sessions.json`, so the daemon's audit
   log can resolve it later even though it can't push):
   ```bash
   python3 ~/hive/hive_watch.py --session-id "<sid>" \
       --session-dir "$HOME/.copilot/session-state/<sid>" register <task-id>
   ```
   `$COPILOT_SESSION_ID` is often **empty** in the shell env — pass `<sid>`
   explicitly (you know it from the session-state path in your context).

2. **Launch a TRACKED async watcher** (bash tool `mode=async`, never
   `setsid`/detached):
   ```bash
   python3 ~/hive/hive_watch.py --session-id "<sid>" \
       --session-dir "$HOME/.copilot/session-state/<sid>" \
       wait <task-id> --interval 15 --timeout 900
   ```
   For several outstanding tasks in one go: `wait --all <id1> <id2> …` blocks
   until every named target is terminal, then exits once. **Gotcha:** `wait`
   snapshots its target set at launch — a task you `register` *after* a `wait`
   is already running is **not** picked up by that running shell. If you queue
   more work while a `wait` is in flight, either include the new id in a fresh
   `wait --all`, or let the current one finish and re-arm.

3. **On the notification:** `read_bash` the watcher shell — its own stdout
   already contains the delivered summary (outcome, commit, log tail; see
   `hive_watch.py`'s `build_content`). Summarize to the human. The
   `inbox_entries` row it also wrote is redundant with this — read it only if
   you want the durable copy, never expect it to have surfaced on its own.

4. **Re-arm on partial completion:** if a `wait --all` batch had targets still
   pending when you need to end the turn, or you have more tasks to add,
   launch a fresh tracked `wait` covering whatever's still outstanding before
   you consider the turn done. Don't let "I already have a wait running"
   excuse leaving newly-added tasks unwatched — check with `hive_watch.py list`.

5. **Cleanup:** `hive_watch.py … unregister <task-id>` once reported. Harmless
   to leave registered (dedup via `inbox/state-<sid>.json`), but tidy is nicer.

Read-only probes / fire-and-forget tasks you won't report on can skip
registration entirely (`--no-wire-hint` on the `add` suppresses the reminder).

> **Recovery check (do this at the start of any session/turn touching the
> hive, or after a `wait` timed out / got reaped):** the notifier daemon's
> audit log is your ground truth for anything that finished while nothing was
> watching. Check `python3 ~/hive/hive_watch.py list` for this session's
> watch/delivered state, or query `inbox_entries` / grep
> `~/hive/inbox/<sid>.jsonl` directly for rows you haven't summarized to the
> human yet. This is the fallback for gaps — it is not, and must never be
> described as, a live push mechanism.

## Capability vocabulary (edit ONLY in `hive_caps.py`)

`os:{windows,linux,darwin}` · `platform:{windows,posix}` · `arch:*` ·
`build:{dotnet,msbuild,go,rust,node}` · `has:{git,python,node,docker}` ·
`role:{builder,tester}` · `name:<lane>` (lane pin).

## Complexity → model tiers (also in `hive_caps.py`)

`--complexity` is required on every add and maps to the model the worker runs:
`1`=claude-haiku-4.5 (simple/mechanical) · `2`=claude-sonnet-5 (standard
coding) · `3`=claude-opus-4.8 (complex/architectural: refactor, debug, security,
PR review) · `auto`=`classify_complexity()` picks 1/2/3. Keyword-based and
conservative (like the caps classifier): architectural/security/refactor keyword
or a very large prompt → 3; short mechanical edit → 1; else 2. (Ground truth
lives in `hive_caps.COMPLEXITY_MODELS`; if it drifts, that dict wins.)

## Sharp edges

- **`dotnet` on Linux is real.** The `hal` box has `dotnet` on PATH, so it
  advertises `build:dotnet` and *will* claim a bare `--require build:dotnet`
  task. That's fine for portable .NET, but **ARS/OI.Ldap builds must also carry
  `os:windows`** (the classifier's ARS rule adds it automatically) or they'll try
  to build on Fedora and fail (SHA1 strong-name + multi-address-connect).
- The classifier is **keyword-based**, deliberately conservative, and only ever
  emits **capability** tags (build/OS/tool). It is **blind to**: locality
  (which box), credentials/tokens on a box, off-PATH tools it can't `which()`,
  network reachability, and data residency. Those never show up as `requires` —
  they are `--worker` pins **you** add (step 2a). It also won't invent tags on an
  ambiguous prompt — that's why step 2 routes `ambiguous` to a human question.
- **A `--worker` pin does NOT bypass the capability gate.** Claim requires
  `requires ⊆ worker.caps` for pinned tasks too. Pin + a require the lane lacks =
  a task nothing can ever claim.
- Worker caps are probed **at startup**. If you install a toolchain on a worker,
  restart it (`systemctl --user restart hive-worker@<lane>`) to re-advertise.
- Adding a new capability = add the tag in `hive_caps._TOOL_CAPS` (probe side)
  **and** `hive_caps._RULES` (classifier side). Same string on both sides or the
  gate never matches.

## Post-mortems — real failures this skill exists to prevent

Grounded in `queue.db` history (the "why step 2a / step 4 exist" file):

| What happened | Root cause | The rule it violates |
|---------------|-----------|----------------------|
| `task-788a6a4` (`smoke_sql1`) pinned `sql1`, `requires=[app:sql]` → stuck `pending` → cancelled | `app:sql` is a **phantom cap** — not in the vocabulary, no live worker probes it. Pin to sql1 did **not** rescue it (gate still checks `requires⊆caps`). | Step 4: validate each `--require` is real *and* satisfiable by a live lane. |
| `task-autotes…` pinned `worker=nonexistent-lane` → never claimed → cancelled | Pinned a lane that doesn't exist. | Step 4: confirm the `--worker` pin is an ONLINE lane. |
| Locality-bound `deploy-*` / NDJ / "you are running on host X" tasks carry **empty** `requires` | The classifier sees no build keyword → `none`. They work **only** because the operator hand-pinned `--worker`. Trusting `none`="portable" would have scattered them onto the wrong box. | Step 2a: locality is your call, not the classifier's. |
| An opencode probe needing the `hal` box's Copilot oauth classified `none`/portable | Credentials are box-state, invisible to a keyword classifier. | Step 2a: reuse-existing-creds ⇒ `--worker` pin. |

The lesson in one line: **`halctl classify` answers "what tools does this need?"
It never answers "which box must this run on?" — that second question is always
yours.**
