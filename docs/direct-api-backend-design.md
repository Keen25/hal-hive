# HAL worker backend — swap copilot-CLI subprocess for a direct Copilot API path

Status: DRAFT / design. Author: HAL (Arpad Fodor). Prior art: `szszeker/lou`
`feature/custom-backend` (`internal/backend/api/`) + ADO PR #94304 (710869 —
"Rebuild AI review on the direct Copilot API").

## Problem

The worker drives every task by shelling out to the `copilot` CLI:

```
copilot -p @prompt --no-ask-user --allow-all --autopilot --output-format json
```

run under `subprocess.run(..., timeout=COPILOT_TIMEOUT_S)` (worker_loop.py
`_copilot_argv` ~155, `run_task` ~562, `run_review_task` ~451). Three costs:

1. **No real cancellation.** `hb.abandon` (heartbeat lost → reaper requeued) is
   only checked *after* the subprocess returns (worker_loop.py:274, checked
   post-run). A cancelled/abandoned task keeps a worker busy until copilot
   finishes or hits `COPILOT_TIMEOUT_S` (default 840s in code; ar1 env 1800s).
   No fast remote kill — confirmed this session (`cancel_task` doesn't interrupt).
2. **Coarse, opaque timeout.** One wall-clock number for the whole agentic run;
   no per-turn budget, no reasoning-budget control, no true `finish_reason`.
3. **Reasoning-model failure mode.** A reasoning model can burn its whole
   completion budget on hidden reasoning and emit empty content — a CLI run just
   times out or returns nothing, indistinguishable from a real error (PR #94304
   observed 32000/32000 tokens, 0 content, 308s).

## Key distinction — two task classes, NOT one

| | Coding task (`run_task`) | Review task (`run_review_task`) |
|---|---|---|
| Shape | **Agentic**: many tool-call turns — read/grep/edit files, run build, `git commit`/`push` | **Single logical output**: read the diff, emit one verdict |
| Output | side effects (branch pushed to mirror) + JSONL usage | `HIVE_REVIEW_VERDICT.json` `{verdict, findings}` (worker_loop.py `_read_review_verdict` ~538) |
| What the CLI provides | the whole **agent loop** (tool schema, tool dispatch, file I/O, git) | just a chat turn that happens to write one file |
| Maps onto direct API? | **No** — you must own the agent loop yourself | **Yes, 1:1** — one streamed `chat/completions`, `response_format:json_object` |

This is the crux. PR #94304 and lou's `api/` client are both **single-turn,
no-tools**. They line up with our review path, not our coding path. Do NOT
conflate them.

## Prior art we are reusing

**lou `internal/backend/api/` (Go, transport layer):**
- `provider.go` — `Provider{ BaseURL(); ApplyAuth(req) }`; add a provider = one file.
- `auth.go` — `TokenProvider` chain: gh-cred-store → `GITHUB_TOKEN` env → static (no subprocess).
- `providers/copilot.go` — `https://api.githubcopilot.com`, `Authorization: Bearer <gh token>`.
- `client.go` / `models.go` — `Complete` / streaming / `ListModels`; chat+tool+reasoning schema (`reasoning_opaque` persisted for multi-turn), multi-modal parts.

**PR #94304 (PowerShell, the recipe that makes the raw API accept a GH token):**
- Auth (`Config.ps1`): LLM call uses `GITHUB_MODELS_TOKEN` (Bearer); ADO write-back uses `SYSTEM_ACCESSTOKEN`.
- Copilot-accept headers (`ApiClient.ps1`): `Copilot-Integration-Id`, `Openai-Intent: conversation-agent`, `X-Initiator: user`, `X-GitHub-Api-Version`, `Accept: text/event-stream`.
- **Stream (SSE), never buffer** — buffered mid-reasoning cutoff returns `content='' finish_reason=''`; streaming yields true `finish_reason` + partial content.
- **`reasoning_effort: low|medium|high`** caps hidden reasoning so the JSON answer is always produced (raising `max_tokens` alone does not).
- **`response_format:{type:json_object}`** guarantees parseable output (prompt must contain the word "JSON").
- Per-family token field: `gpt-5*` → `max_completion_tokens`; others → `max_tokens` (`Get-AiMaxTokensField`).
- Wall-clock `CancellationTokenSource` bounds *every* streaming read (StreamReader won't observe the token on its own) — kills a stalled mid-stream socket.

## Proposal — a pluggable worker backend, phased

Introduce a `Backend` seam in the worker so the run mechanism is swappable per
task, defaulting to today's CLI (zero behaviour change until opted in).

```
Backend.run_review(task, run_dir) -> {verdict, findings, usage, finish_reason}
Backend.run_coding(task, run_dir) -> {pushed_ref?, exit_code, usage, ...}
```

Backends:
- `cli` (existing) — `_copilot_argv` subprocess. Default.
- `api` (new) — direct `chat/completions`.

### Phase A — review path on the direct API (low risk, high value) — DO FIRST

Review is already single-turn JSON. Replace `run_review_task`'s subprocess with
one streamed `chat/completions`:

- Messages: system = current review contract prompt (already forbids edits/tools,
  already asks for `{verdict, findings}` JSON) + user = the diff (we already
  `git checkout` the branch; feed `git diff <base>...<head>` instead of letting
  copilot fetch it).
- `response_format:{type:json_object}` → parse straight into the verdict; drop the
  `HIVE_REVIEW_VERDICT.json` file dance. Keep the **fail-safe to REQUEST_CHANGES**
  on any parse/HTTP/timeout error (worker_loop.py ~538, `_read_review_verdict`).
- `reasoning_effort` from complexity tier (cx3 → `medium`, else `low`) so opus
  reviewers always emit the verdict.
- **Real cancellation**: run the HTTP call on a thread with a
  `threading.Event`/deadline that also watches `hb.abandon`; on abandon, cancel
  the request (close the response stream) and return immediately — the fast
  remote kill we lack today.
- Usage: read `copilot_usage.total_nano_aiu` from the final SSE frame → same
  usage rollup we parse from the JSONL today (`_parse_usage_from_log` ~81).

Payoff without touching the agentic path: true finish_reason, reasoning budget,
sub-second cancel, and reviewers stop consuming a full CLI process.

### Phase B — coding path (larger; agent loop ownership)

To drop the CLI for coding we must own the loop the CLI gives us free. Two routes:

- **B1 — embed lou's Go harness as an alternate worker backend.** lou already has
  the agentic pieces beyond `api/` (`internal/backend/copilot*.go`, `assembly/`,
  `conversation/loop.go`, MCP wiring). Ship a small Go `lou`-backed runner the
  worker execs (still a subprocess, but *ours* — cancellable, JSON I/O, tool
  policy in our hands). Lower build cost than reimplementing; adds a Go toolchain
  dependency on builder lanes.
- **B2 — Python agent loop in-worker.** Reuse Phase A's `api` client, add a
  tool schema (read/write/grep/bash/git) + dispatcher executing in `run_dir`.
  Full control, no Go dep, but this is a real agent runtime to build, harden, and
  keep at parity with copilot's tool set. Bigger, riskier.

Recommendation: ship Phase A, run it in production on reviews for a few weeks,
then decide B1 vs B2 with real cancel/latency/cost data. Do not gate Phase A on
Phase B.

## Auth / headers (both phases)

- LLM token: reuse the gh cred store (lou's `GhTokenProvider`) or a
  `HIVE_COPILOT_API_TOKEN` env, chained (gh → env). No subprocess.
- Required request headers to make `api.githubcopilot.com` accept it:
  `Authorization: Bearer <tok>`, `Accept: text/event-stream`,
  `Copilot-Integration-Id: <id>`, `Openai-Intent: conversation-agent`,
  `X-Initiator: user`, `X-GitHub-Api-Version: <ver>`. (Values per PR #94304
  `Config.ps1`/`ApiClient.ps1`.)
- ADO write-back (promote/thread posts) stays on the az AAD token
  (`499b84ac-...`) — unchanged.

## Config / flags

- `HIVE_BACKEND=cli|api` (global default) + per-task `backend` column (optional
  override, mirrors how `repo`/`model`/`complexity` already ride the task row).
- `HIVE_COPILOT_API_TOKEN`, `HIVE_COPILOT_ENDPOINT`
  (default `https://api.githubcopilot.com/chat/completions`),
  `HIVE_COPILOT_API_VERSION`, `HIVE_COPILOT_INTEGRATION_ID`.
- `reasoning_effort` derived from `complexity`; overridable via
  `HIVE_REASONING_EFFORT`.
- Version bump: adding the `backend` column = `SCHEMA_VERSION` +1 + `init_db`
  migration (mirror the `repo` column migration, hive_queue.py); `CODE_VERSION`
  +1 and roll the fleet (self-update already proven this session).

## Risks / open questions

- **Copilot-Integration-Id policy.** PR #94304 runs in CI with a sanctioned
  integration id. Confirm the same id/intent is acceptable for HAL's use (ToS)
  before Phase A goes live — this is the one hard gate.
- **Review-quality parity.** Single-turn diff review can't open other files the
  way an agentic reviewer can. Fine for diff-scoped review (our current prompt is
  already diff-scoped); note it, don't expand scope silently.
- **Phase B tool-parity drift.** Any home-grown loop (B2) must track copilot's
  tool behaviour or coding output regresses — real maintenance cost. B1 avoids it.
- **Windows builder lanes.** Phase B on ar1 still needs the VS2022 toolchain for
  ARS builds regardless of backend — the API swap changes the *driver*, not the
  build.

## Validation plan (Phase A)

1. Unit: mock SSE stream → assert verdict parse, fail-safe on error/timeout,
   cancel on `hb.abandon`.
2. Shadow: run `api` review alongside the `cli` review on the same branch for N
   tasks; diff verdicts + findings; compare latency, cancel-responsiveness, cost
   (`total_nano_aiu`).
3. Flip `HIVE_BACKEND=api` for reviews only once shadow agrees; coding stays
   `cli`.

## Anchors (current code)

- `_copilot_argv` worker_loop.py ~155; perms ~125.
- `run_task` ~562 (agentic); `run_review_task` ~451; `_read_review_verdict` ~538.
- `_parse_usage_from_log` ~81 (usage rollup source).
- abandon signal: `Heartbeat.abandon` ~265/274, checked post-run.
- schema/migration pattern: hive_queue.py `init_db` (repo column) — model the
  `backend` column on it.
