# SPEC — Build + test verify of PR !95956 deep-review CLEAN-UP commit

You are mini-HAL on ar1. This is a VERIFY task. Your clone's base branch
`feature/723849_thread-fixes-verify` now points at commit `af03880340`, which is
the PR !95956 feature HEAD plus round-1 fixes (2138b3c6) plus round-2 panel fixes
(4afa282c16) plus the **deep-review clean-up** (9612b70). **Do NOT fetch from ADO
origin; do NOT modify any tracked source.** The only file you may create is the
report described at the end.

## Content sanity gate (do this FIRST; if any check fails, STOP and report)
Confirm every clean-up change is present in the checked-out tree.

1. **Retry-budget alignment (679974)** — the mirror budget constant is present:
   `git grep -n "SecurityMigrationOtherSqlRetries = 1" -- Service/Source/Service.Configuration/Database/ManagementHistory/MhMigrationManager.cs`
2. **Retry heartbeat (679974)** — the policy call now passes the named budget +
   an actionOnRetry (no bare `() => { }` for the retry action):
   `git grep -n "ActionOnRetry, ActionOnFallback, SecurityMigrationTransientRetries" -- Service/Source/Service.Configuration/Database/ManagementHistory/MhMigrationManager.cs`
3. **679974** resiliency wrap still present:
   `git grep -n "securityMigrationPolicy.Execute" -- Service/Source/Service.Configuration/Database/ManagementHistory/MhMigrationManager.cs`
4. **Timeout unification (679972, Finding 6)** — `ResolveConnectTimeout` is now
   applied ONCE in CheckSqlConnection, before the auth branching (exactly 1 match
   in the file, and it must NOT be inside the ManagedIdentity branch):
   `git grep -cn "ResolveConnectTimeout(" -- Service/Source/Service.Configuration/PowerShell/Parameters/DatabaseParameters.cs`
   (expect a count of 1)
5. **679972** all three sites still route through the resolver:
   `git grep -n "ResolveConnectTimeout(" -- Service/Source/Service.Configuration/Database/SQLHelpers.cs Service/Source/Service.Configuration/Database/ManagementHistory/DatabaseConfig.cs Service/Source/Service.Configuration/PowerShell/Parameters/DatabaseParameters.cs`
   (expect one match in each of the three files)
6. **Doc-comment fix (679973, Finding 5)** — the inaccurate "trailing whitespace"
   example is GONE from the interface doc comment (this grep must return NOTHING):
   `git grep -n "trailing whitespace" -- Common/Source/Configuration.Common/IConfigurationProvider.cs`
   (expect zero matches)
7. **679973** MSF read sites still route through TryGetBool (>= 3 across
   ARServiceHelper):
   `git grep -cn "ConfigProvider.TryGetBool(" -- Service/Source/Service.Configuration/Service/ARServiceHelper.cs`
   (expect a count of 3)
8. **Renamed test (Finding 4)** — new name present, OLD name gone:
   `git grep -n "MakeConnectionString_NoAdvancedProperties_FloorsUnsetTimeoutToShared60sDefault" -- Service/Tests/Service.Configuration.Tests/Database/ManagementHistory/DatabaseConfigManagedIdentityTests.cs`
   `git grep -n "MakeConnectionString_NoAdvancedProperties_LeavesBuilderDefaults" -- Service/Tests` (expect ZERO matches)
9. **Strengthened disposal test (Finding 8)** — asserts the exact exception
   instance propagates:
   `git grep -n "Assert.AreSame(expected, actual" -- Service/Tests/Service.Configuration.Tests/Database/ManagementHistory/DatabaseConfigManagedIdentityTests.cs`
10. **New regression tests present (Findings 6 + 7):**
    `git grep -n "CheckSqlConnection_ManagedIdentity_UnsetConnectionTimeout_UsesShared60sDefault" -- Service/Tests/Service.Configuration.Tests/Database/SqlHelpersSecurityTypeTests.cs`
    `git grep -n "GetAdvancedDatabaseServiceData_NonCanonicalMultiSubnetFailover_FallsBackToNull" -- Service/Tests/Service.Configuration.Tests/Service/ARServiceHelperConfigurationProviderTests.cs`
    `git grep -n "GetAdvancedDatabaseServiceData_CanonicalMultiSubnetFailover_ReadsBothSections" -- Service/Tests/Service.Configuration.Tests/Service/ARServiceHelperConfigurationProviderTests.cs`

If all match, proceed.

## Build (Debug|x64) — BOTH solutions, 0 errors required
Resolve VS2022 MSBuild (via `vswhere` or the known `D:\VS2022` path used in prior
ARS tasks). From the repo root of THIS clone build, in order:

1. Service solution (includes the native C++ `Service.Native`):
   ```
   & "<MSBuild.exe>" Service\ActiveRoles.Service.sln /p:Configuration=Debug /p:Platform=x64 /m /v:m
   ```
2. ConfigCenter solution (glob the .sln under `ConfigCenter\`):
   ```
   & "<MSBuild.exe>" ConfigCenter\<ConfigCenter solution>.sln /p:Configuration=Debug /p:Platform=x64 /m /v:m
   ```

Require **0 errors** for each. Record error + warning counts per solution. On
failure capture the exact `error CS####` / `error MSB####` / `error C####`
(native) / `error LGHT####` (WiX) lines. If you hit `NETSDK1004
project.assets.json not found` for a project, run its one-time restore
(`MSBuild <that>.csproj /t:Restore`) then rebuild.

## Test — MI regression suite, all green (MUST include the new/changed tests)
Find the built `ActiveRoles.Service.Configuration.Tests.dll` and run the MI
classes with vstest.console.exe:
```
& "<vstest.console.exe>" <ActiveRoles.Service.Configuration.Tests.dll> `
  /TestCaseFilter:"FullyQualifiedName~ManagedIdentity|FullyQualifiedName~SqlHelpersSecurityType|FullyQualifiedName~ARServiceHelper|FullyQualifiedName~DatabaseConfig"
```
Require all green. In particular these NEW/CHANGED tests must pass:
- `MakeConnectionString_NoAdvancedProperties_FloorsUnsetTimeoutToShared60sDefault` (asserts 60)
- `CreateConnection_ManagedIdentity_TokenAcquisitionFailure_DisposesConnectionAndPropagates` (now asserts AreSame on the propagated exception)
- `CheckSqlConnection_ManagedIdentity_UnsetConnectionTimeout_UsesShared60sDefault` (asserts the unset MI timeout resolves to 60s)
- `CheckSqlConnection_ManagedIdentity_FeedsConfiguredConnectionTimeoutToTokenBudget` (explicit 42 still preserved — regression guard for the Finding-6 move)
- `GetAdvancedDatabaseServiceData_NonCanonicalMultiSubnetFailover_FallsBackToNull`
- `GetAdvancedDatabaseServiceData_CanonicalMultiSubnetFailover_ReadsBothSections`
- `ReadDatabaseParameters_NonCanonicalMultiSubnetFailover_FallsBackToNull` (unchanged; must still pass)
Record passed/failed/skipped totals and the status of each named test above.

## Report (REQUIRED — this is the task's only tracked change)
Write `verify-report-95956-threads-round3.md` at the repo root containing:
- the content-gate grep results (pass/fail each, with the match line/count)
- build: per-solution errors / warnings counts (and error lines if any)
- test: passed / failed / skipped totals + status of each named test above + any
  failing test names
- any restore step you ran
- a final one-line verdict: `VERIFY PASS` or `VERIFY FAIL: <reason>`

Stage/commit that single file so it is captured. Do NOT touch any other file.

## Do NOT
Do not fetch ADO origin, do not edit source, do not push to any ADO branch. The
worker harness handles pushing your report to the throwaway task branch in the
hive mirror only.
