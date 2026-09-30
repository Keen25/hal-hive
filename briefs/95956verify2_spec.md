# SPEC — Build + test verify of PR !95956 MI fixes (mirror content branch)

You are mini-HAL on ar1. This is a VERIFY task. Your clone already contains the
target source (base branch `feature/723849_verify-content` = the exact tree of
ADO commit 8018a74: base 595b1f4d + the PR !95956 review fixes). **Do NOT fetch
from ADO origin; do NOT modify any tracked source.** The only file you may
create is the report described at the end.

## Content sanity gate (do this FIRST; if any check fails, STOP and report)
Confirm the four review fixes are present in the checked-out tree:
1. `git grep -n "internal static int ReduceConnectTimeoutBudget" -- Service/Source/Service.Configuration/Database/ManagedIdentitySql.cs` — must match.
2. `git grep -n "IsTransientProvisioningError" -- Service/Source/Service.Configuration/Database/NewDatabaseAction.cs` — must match.
3. `git grep -n "ProtocolSchemes" -- Service/Source/Service.Configuration/Database/SqlAccessTokenProvider.cs` — must match.
4. `git grep -n "IsCancellation" -- Service/Source/Service.Configuration/Database/CachingSqlAccessTokenProvider.cs` — must match.
5. `git grep -n "NormalizeHost_AllAlphabeticIpv6Literal" -- Service/Tests/Service.Configuration.Tests/Database/SqlHelpersSecurityTypeTests.cs` — must match.
If all five match, proceed. (There is no SHA to check — this branch carries the
content, not the ADO commit hash.)

## Build (Debug|x64)
Resolve VS2022 MSBuild (via `vswhere` or the known `D:\VS2022` path used in prior
ARS tasks) and build the Service solution from the repo root of THIS clone:
```
& "<MSBuild.exe>" Service\ActiveRoles.Service.sln /p:Configuration=Debug /p:Platform=x64 /m /v:m
```
Require **0 errors**. Record error + warning counts. On failure, capture the exact
`error CS####` / `error MSB####` lines. If you hit `NETSDK1004 project.assets.json
not found` for a project, run its one-time restore (`MSBuild <that>.csproj
/t:Restore`) then rebuild — same as prior ARS tasks.

## Test
Find the built `ActiveRoles.Service.Configuration.Tests.dll` and run these four
classes with vstest.console.exe:
```
& "<vstest.console.exe>" <ActiveRoles.Service.Configuration.Tests.dll> `
  /TestCaseFilter:"FullyQualifiedName~ManagedIdentitySqlTests|FullyQualifiedName~SqlHelpersSecurityTypeTests|FullyQualifiedName~ManagedIdentityConnectTimeoutBudgetTests|FullyQualifiedName~NewDatabaseActionTransientClassifierTests"
```
Require all green. Explicitly confirm these ran and passed:
- `CachingProvider_WrappedCancellationTimeout_AllowsLargerBudgetRetry`
- `NormalizeHost_AllAlphabeticIpv6Literal_NotTreatedAsScheme`, `NormalizeHost_GenuineTcpScheme_StillStripped`, `NormalizeHost_AdminScheme_StillStripped`
- all of `ManagedIdentityConnectTimeoutBudgetTests`
- all of `NewDatabaseActionTransientClassifierTests`

## Report (REQUIRED — this is the task's only tracked change)
Write a file `verify-report-95956.md` at the repo root containing:
- the five content-gate grep results (pass/fail each)
- build: errors / warnings counts (and error lines if any)
- test: passed / failed / skipped totals + explicit pass/fail for each named test above
- any restore step you ran
- a final one-line verdict: `VERIFY PASS` or `VERIFY FAIL: <reason>`
Then stage/commit that single file so it is captured. Do NOT touch any other file.

## Do NOT
Do not fetch ADO origin, do not edit source, do not push to any ADO branch. The
worker harness handles pushing your report to the throwaway task branch in the
hive mirror only.
