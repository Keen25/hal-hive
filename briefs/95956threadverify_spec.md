# SPEC — Build + test verify of PR !95956 review-thread fixes

You are mini-HAL on ar1. This is a VERIFY task. Your clone already contains the
target source: base branch `feature/723849_thread-fixes-verify` = the PR !95956
feature HEAD (58d37dc) **plus** the fixes for all six open review threads
(678392, 678393, 678428, 679749, 679750, 679751). **Do NOT fetch from ADO
origin; do NOT modify any tracked source.** The only file you may create is the
report described at the end.

## Content sanity gate (do this FIRST; if any check fails, STOP and report)
Confirm every thread fix is present in the checked-out tree. Each `git grep`
MUST match:

1. **679749** retry-exhaustion now throws:
   `git grep -n "lastTransientError" -- Service/Source/Service.Configuration/Database/NewDatabaseAction.cs`
2. **678428** replication string preserves MI shape:
   `git grep -n "builder.TrustServerCertificate = curBuilder.TrustServerCertificate" -- Service/Source/Service.Database/DbUtils/ConnectionString.cs`
3. **679750** MH import timeout/MSF cmdlet params:
   `git grep -n "SourceConnectionTimeout" -- Service/Source/Service.Configuration/PowerShell/Commands/ImportARDatabaseCommand.cs`
4. **679751** MSI ships the two Azure.Identity transitive deps:
   `git grep -n "Microsoft.IdentityModel.Abstractions.dll" -- Setup/Setup.ConfigCenter/Product.wxs`
5. **678393** ConfigCenter upgrade MI helper exists:
   `git grep -n "class ManagedIdentityUpgradeConnection" -- ConfigCenter/Configuration.UI/InPlaceUpgrade/ManagedIdentityUpgradeConnection.cs`
6. **678392a** MI client-id password-only strip:
   `git grep -n "DeletePasswordFromRegistry" -- Service/Source/Service.Configuration/Database/ArsOleDbConnectionString.cs`
7. **678392b** MI read-back branch in ReadDatabaseParameters:
   `git grep -n "else if (dwValue == SecurityTypes.ManagedIdentity)" -- Service/Source/Service.Configuration/Service/ARServiceHelper.cs`
8. **678392c** native re-emits DbLogin as User ID for MI:
   `git grep -n "sqlString->UserID = CStringToMString(strValue)" -- Service/Source/Service.Native/Include/NativeArsDatabase.h`
   (expect 3 matches: SQL, Azure-password, and the new MI branch)

If all match, proceed. There is no ADO commit hash to check — this branch
carries the content, not the PR commit.

## Build (Debug|x64) — BOTH solutions, 0 errors required
Resolve VS2022 MSBuild (via `vswhere` or the known `D:\VS2022` path used in prior
ARS tasks). From the repo root of THIS clone build, in order:

1. Service solution (includes the native C++ `Service.Native` — this exercises
   the `NativeArsDatabase.h` MI edit):
   ```
   & "<MSBuild.exe>" Service\ActiveRoles.Service.sln /p:Configuration=Debug /p:Platform=x64 /m /v:m
   ```
2. ConfigCenter solution (exercises the `ManagedIdentityUpgradeConnection.cs`
   new file + the credential-forwarding edits in Configuration.UI /
   Configuration.Common):
   ```
   & "<MSBuild.exe>" ConfigCenter\<ConfigCenter solution>.sln /p:Configuration=Debug /p:Platform=x64 /m /v:m
   ```
   Locate the ConfigCenter .sln under `ConfigCenter\` (glob `*.sln`).

Require **0 errors** for each. Record error + warning counts per solution. On
failure capture the exact `error CS####` / `error MSB####` / `error C####`
(native) / `error LGHT####` (WiX) lines. If you hit `NETSDK1004
project.assets.json not found` for a project, run its one-time restore
(`MSBuild <that>.csproj /t:Restore`) then rebuild — same as prior ARS tasks.

## Test — MI regression suite, all green
Find the built `ActiveRoles.Service.Configuration.Tests.dll` and run the MI
classes with vstest.console.exe:
```
& "<vstest.console.exe>" <ActiveRoles.Service.Configuration.Tests.dll> `
  /TestCaseFilter:"FullyQualifiedName~ManagedIdentity|FullyQualifiedName~SqlHelpersSecurityType|FullyQualifiedName~ARServiceHelper"
```
Require all green (these guard the MI shape + persistence paths my edits touch).
If a Configuration.Tests assembly is not produced, note it and run whatever MI
test assembly the Service solution builds. Record passed/failed/skipped totals.

## Report (REQUIRED — this is the task's only tracked change)
Write `verify-report-95956-threads.md` at the repo root containing:
- the eight content-gate grep results (pass/fail each, with the match line)
- build: per-solution errors / warnings counts (and error lines if any)
- test: passed / failed / skipped totals + any failing test names
- any restore step you ran
- a final one-line verdict: `VERIFY PASS` or `VERIFY FAIL: <reason>`

Stage/commit that single file so it is captured. Do NOT touch any other file.

## Do NOT
Do not fetch ADO origin, do not edit source, do not push to any ADO branch. The
worker harness handles pushing your report to the throwaway task branch in the
hive mirror only.
