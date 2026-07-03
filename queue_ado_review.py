#!/usr/bin/env python3
"""Queue an ADO pull-request review task with the diff pre-fetched on HAL.

Why this exists: hive workers run headless (systemd service / WinSW) and cannot
complete Azure DevOps' interactive browser auth, so they cannot fetch a PR
themselves. HAL, however, is already `az login`'d and can mint an ADO access
token non-interactively. So HAL fetches the PR metadata + a real unified diff
here and embeds it in the task prompt; the worker that picks the task up writes
the review purely from the provided context and needs no ADO access at all.

No PAT is stored or transferred — the az token is short-lived and never leaves
HAL (it is not written into the queued prompt).

Usage:
    python3 queue_ado_review.py 94406 [--priority 3] [--worker ar1]
"""
import argparse
import difflib
import json
import subprocess
import sys
import urllib.parse
import urllib.request

ORG = "https://dev.azure.com/1id"
PROJECT = "Active Roles"
REPO = "ActiveRoles"
# Azure DevOps resource GUID — mints a token scoped to ADO.
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
API = "api-version=7.1"
# Per-file and total diff caps so a huge PR does not blow up the prompt.
MAX_FILE_DIFF_LINES = 400
MAX_TOTAL_DIFF_CHARS = 60000


def az_token() -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", ADO_RESOURCE,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        sys.exit(f"failed to mint ADO token (is `az login` valid?): {out.stderr.strip()}")
    return out.stdout.strip()


def _base() -> str:
    proj = urllib.parse.quote(PROJECT)
    return f"{ORG}/{proj}/_apis/git/repositories/{REPO}"


def _get(url: str, token: str, as_text: bool = False):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if as_text:
        return data.decode("utf-8", "replace")
    return json.loads(data)


def _item_at(path: str, commit: str, token: str) -> str:
    """Full text of a file at a commit, or '' if it does not exist there."""
    q = urllib.parse.urlencode({
        "path": path,
        "versionDescriptor.version": commit,
        "versionDescriptor.versionType": "commit",
        "$format": "text",
        "api-version": "7.1",
    })
    url = f"{_base()}/items?{q}"
    try:
        return _get(url, token, as_text=True)
    except Exception:
        return ""  # added/deleted on one side


def build_review_prompt(pr_id: int, token: str) -> str:
    b = _base()
    pr = _get(f"{b}/pullRequests/{pr_id}?{API}", token)
    title = pr.get("title", "")
    desc = (pr.get("description") or "").strip()
    src_ref = pr.get("sourceRefName", "")
    tgt_ref = pr.get("targetRefName", "")
    src_commit = (pr.get("lastMergeSourceCommit") or {}).get("commitId", "")
    tgt_commit = (pr.get("lastMergeTargetCommit") or {}).get("commitId", "")

    iters = _get(f"{b}/pullRequests/{pr_id}/iterations?{API}", token)["value"]
    iter_id = iters[-1]["id"]
    changes = _get(
        f"{b}/pullRequests/{pr_id}/iterations/{iter_id}/changes?{API}", token
    ).get("changeEntries", [])

    diff_blocks, total = [], 0
    file_list = []
    for c in changes:
        item = c.get("item", {})
        path = item.get("path")
        if not path or item.get("isFolder"):
            continue
        ctype = c.get("changeType", "edit")
        file_list.append(f"{ctype:<8} {path}")
        old = "" if ctype == "add" else _item_at(path, tgt_commit, token)
        new = "" if ctype == "delete" else _item_at(path, src_commit, token)
        ud = list(difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=f"a{path}", tofile=f"b{path}", lineterm="",
        ))
        if len(ud) > MAX_FILE_DIFF_LINES:
            ud = ud[:MAX_FILE_DIFF_LINES] + [
                f"... [diff truncated at {MAX_FILE_DIFF_LINES} lines] ..."]
        block = "\n".join(ud) if ud else "(binary or no textual diff)"
        if total + len(block) > MAX_TOTAL_DIFF_CHARS:
            diff_blocks.append(f"### {path}\n(omitted — total diff size cap reached)")
            continue
        total += len(block)
        diff_blocks.append(f"### {path}  [{ctype}]\n```diff\n{block}\n```")

    files_md = "\n".join(file_list)
    diffs_md = "\n\n".join(diff_blocks)
    pr_url = (f"{ORG}/{urllib.parse.quote(PROJECT)}/_git/{REPO}/"
              f"pullrequest/{pr_id}")

    return f"""You are reviewing Azure DevOps pull request {pr_id}.
The PR and its full unified diff are embedded below — you already have everything
you need; do NOT attempt to access Azure DevOps or any network resource.

PR: {pr_id} — {title}
URL: {pr_url}
Source: {src_ref}  ->  Target: {tgt_ref}

Description:
{desc or '(none)'}

Changed files ({len(file_list)}):
{files_md}

--- FULL DIFF ---
{diffs_md}
--- END DIFF ---

Write a thorough code review to a NEW file reviews/pr-{pr_id}.md at the repo root
(create the reviews/ directory if needed). Structure it exactly as:

# PR {pr_id} Review — <one-line summary of what the PR changes>

## Findings
Concrete issues only — bugs, logic errors, security concerns, breaking API or
behavioural changes, missing/weak tests. Each finding cites `path:line` from
the diff and is ordered by severity (Critical > High > Medium > Low). High
signal-to-noise: do NOT comment on style, formatting, or naming.

## Risk
Assess blast radius and what could break in production.

## Recommendation
Approve or Request changes, with a one-paragraph justification.

Only create or modify reviews/pr-{pr_id}.md. Do not touch any other file."""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pr_id", type=int)
    ap.add_argument("--priority", type=int, default=3)
    ap.add_argument("--worker", default=None, help="pin to a lane (optional)")
    args = ap.parse_args()

    token = az_token()
    prompt = build_review_prompt(args.pr_id, token)

    cmd = ["python3", "halctl.py", "add", prompt,
           "--priority", str(args.priority)]
    if args.worker:
        cmd += ["--worker", args.worker]
    print(f"[queue-ado-review] prompt assembled ({len(prompt)} chars) — adding task")
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
