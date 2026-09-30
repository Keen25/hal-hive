# SPEC — Build + test verify of PR !95956 fixes (feature/723849, tip 8018a74)

You are mini-HAL on ar1. Repo: `D:\src\ActiveRoles`. This is a VERIFY-ONLY task.
**Do NOT push, do NOT commit, do NOT modify any tracked source.** Only fetch,
checkout, build, and run tests, then report.

## Goal
Prove that branch `feature/723849_runtime-mi-token-injection` at commit
`8018a74df34b1bb666a61192139002105abfc1e1` builds clean and that the Managed
Identity SQL unit tests pass. This commit added 4 review-fix changes + tests to
`Service.Configuration` and `Service.Configuration.Tests`.

## Checkout
1. `git fetch origin`
2. `git checkout feature/723849_runtime-mi-token-injection`
3. `git reset --hard origin/feature/723849_runtime-mi-token-injection`
4. Confirm: `git rev-parse HEAD` MUST print
   `8018a74df34b1bb666a61192139002105abfc1e1`. If it does not, STOP and report
   the actual SHA (do not proceed).

## Build (Debug|x64)
Use the same toolchain as prior ARS tasks (VS2022 at `D:\VS2022`, resolve via
`vswhere` if needed):
```
& "D:\VS2022\MSBuild\Current\Bin\MSBuild.exe" Service\ActiveRoles.Service.sln `
  /p:Configuration=Debug /p:Platform=x64 /m /v:m
```
Require **0 errors**. Report error + warning counts. On failure, capture the exact
`error CS####` / `error MSB####` lines — that is the important signal. If you hit
`NETSDK1004 project.assets.json not found` for any project, run the one-time
restore for that project (`MSBuild <that>.csproj /t:Restore`) exactly as done
before, then rebuild.

## Test
Locate the built `ActiveRoles.Service.Configuration.Tests.dll` (under
`Service\Tests\Service.Configuration.Tests\bin\x64\Debug\...` or the solution
output). Run these four test classes with vstest:
```
& "D:\VS2022\Common7\IDE\CommonExtensions\Microsoft\TestWindow\vstest.console.exe" `
  <ActiveRoles.Service.Configuration.Tests.dll> `
  /TestCaseFilter:"FullyQualifiedName~ManagedIdentitySqlTests|FullyQualifiedName~SqlHelpersSecurityTypeTests|FullyQualifiedName~ManagedIdentityConnectTimeoutBudgetTests|FullyQualifiedName~NewDatabaseActionTransientClassifierTests"
```
Require all green. Explicitly confirm these new tests ran and passed:
- `CachingProvider_WrappedCancellationTimeout_AllowsLargerBudgetRetry`
- `NormalizeHost_AllAlphabeticIpv6Literal_NotTreatedAsScheme`,
  `NormalizeHost_GenuineTcpScheme_StillStripped`, `NormalizeHost_AdminScheme_StillStripped`
- all of `ManagedIdentityConnectTimeoutBudgetTests`
- all of `NewDatabaseActionTransientClassifierTests`

If the whole-assembly run is cheap, run it too and report totals.

## Do NOT push / do NOT modify
Leave the tree as-is (checked out at 8018a74). Do not stage, commit, or push
anything. This is a read-only verification.

## Report
Report, concisely:
- `git rev-parse HEAD` (must be 8018a74…)
- build result: errors / warnings counts (and any error lines if non-zero)
- test result: passed / failed / skipped totals, plus explicit pass/fail for the
  named new tests above
- any restore step you had to run
