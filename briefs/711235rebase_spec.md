# SPEC — Rebase 711235 spike onto updated 725181 tip, rebuild, retest

You are mini-HAL on ar1. Repo: `D:\src\ActiveRoles`. Do the following exactly.
Do NOT push (master-HAL force-pushes via token). Do NOT improvise on conflicts.

## Goal
Rebase `feature/711235_ndj-runtime-composition-spike` (currently based on the OLD
725181 tip `55cd34a101`) onto the NEW 725181 tip `0330f3a1c6` (the PR #94311
review fixes), then prove it still builds and its tests pass on the new base.

## Steps
1. `git fetch origin` (get the latest `origin/feature/725181_oi-ldap-dirsync-change-tracker`
   = `0330f3a1c6`, and `origin/feature/711235_ndj-runtime-composition-spike`).
2. Check out the spike branch and make sure it matches origin:
   `git checkout feature/711235_ndj-runtime-composition-spike`
   `git reset --hard origin/feature/711235_ndj-runtime-composition-spike`
3. Record the pre-rebase SHA (`git rev-parse HEAD`) and the merge-base with the
   new base (`git merge-base HEAD origin/feature/725181_oi-ldap-dirsync-change-tracker`).
4. Rebase onto the new base:
   `git rebase origin/feature/725181_oi-ldap-dirsync-change-tracker`
   - **If the rebase reports conflicts: run `git rebase --abort`, do NOT try to
     resolve them, and STOP — report the conflicting files and the exact git
     output.** (No conflicts are expected: the fix commit touched
     `OiLdapDirSyncChangeTracker.cs`, `DirectorySynchronizer.cs`,
     `OiLdapDirSyncChangeTrackerTests.cs`; the spike touched
     `ClientConnections.cs`, `DirectoryDiscovery.cs`, `Service.csproj` + new files.
     If it's clean, continue.)
5. Confirm the new history: `git log --oneline -4` (the spike commit `ce4b8b0d29`'s
   content should now sit on top of `0330f3a1c6`; the SHA will change — that's
   expected for a rebase).

## Build (full solution, Debug|x64)
Use the same toolchain you used last time (VS2022 at `D:\VS2022`, resolve via
`vswhere` if needed):
```
& "D:\VS2022\MSBuild\Current\Bin\MSBuild.exe" Service\ActiveRoles.Service.sln `
  /p:Configuration=Debug /p:Platform=x64 /m /v:m
```
Require **0 errors**. Report the error/warning counts. If the build fails, capture
the exact `error CS####`/`error MSB####` lines — a failure here is the important
signal (e.g. the spike referencing something the fix renamed, like the old
`DirSyncReplicationRightsException`). If you hit `NETSDK1004 project.assets.json
not found` for `Common.Services.ActiveDirectory`, run the one-time restore
(`MSBuild ...\Common.Services.ActiveDirectory.csproj /t:Restore`) exactly as
before, then rebuild.

## Test
Run BOTH suites (the spike's tests AND the tracker tests, to confirm the rebase
didn't disturb either):
```
& "D:\VS2022\Common7\IDE\CommonExtensions\Microsoft\TestWindow\vstest.console.exe" `
  <ActiveRoles.Service.Tests.dll> /Tests:NdjRuntimeComposition,OiLdapDirSyncChangeTrackerTests
```
Require all green. Report totals (passed/failed) and any failure detail.

## Do NOT push
Leave the rebased branch checked out locally. master-HAL force-pushes with
`--force-with-lease` via the AAD token.

## Report
Write `D:\tmp\711235rebase_report.md` with:
- pre-rebase SHA, new HEAD SHA after rebase, and the `git log --oneline -4`.
- whether the rebase was clean or conflicted (list files if conflicted).
- build result (errors/warnings; any new errors vs the pre-rebase build).
- test totals (both suites) and any failures.
- anything that broke specifically because of the new base (name it).
