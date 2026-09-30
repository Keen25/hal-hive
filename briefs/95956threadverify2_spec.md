# SPEC — Build + test verify of PR !95956 SECOND-ROUND review-thread fixes

You are mini-HAL on ar1. This is a VERIFY task. Your clone's base branch
`feature/723849_thread-fixes-verify` now points at commit `4afa282c16`, which is
the PR !95956 feature HEAD (58d37dc) **plus** the first-round thread fixes
(2138b3c6) **plus** the second-round panel fixes (679970-679974). **Do NOT fetch
from ADO origin; do NOT modify any tracked source.** The only file you may create
is the report described at the end.

## Content sanity gate (do this FIRST; if any check fails, STOP and report)
Confirm every second-round fix is present in the checked-out tree. Each
`git grep` MUST match:

1. **679973** safe boolean read seam on the interface:
   `git grep -n "bool TryGetBool(string section, string key, out bool value)" -- Common/Source/Configuration.Common/IConfigurationProvider.cs`
2. **679973** registry impl uses bool.TryParse (non-canonical -> unset):
   `git grep -n "if (bool.TryParse(raw.ToString(), out value))" -- Common/Source/Configuration.Common/RegistryConfigurationProvider.cs`
3. **679973** MSF read sites now route through TryGetBool (expect >= 3 matches
   across ARServiceHelper: GetAdvancedDatabaseServiceData x2 + ReadDatabaseParameters):
   `git grep -cn "ConfigProvider.TryGetBool(" -- Service/Source/Service.Configuration/Service/ARServiceHelper.cs`
   (expect a count of 3)
4. **679972** shared connect-timeout resolver exists:
   `git grep -n "internal static int ResolveConnectTimeout(int? configured)" -- Service/Source/Service.Configuration/Database/SQLHelpers.cs`
5. **679972** all three sites route through it (SQLHelpers.GetConnectionString,
   DatabaseConfig.MakeConnectionString, DatabaseParameters.CheckSqlConnection):
   `git grep -n "ResolveConnectTimeout(" -- Service/Source/Service.Configuration/Database/SQLHelpers.cs Service/Source/Service.Configuration/Database/ManagementHistory/DatabaseConfig.cs Service/Source/Service.Configuration/PowerShell/Parameters/DatabaseParameters.cs`
   (expect one match in each of the three files)
6. **679974** MigrateSecurityDbObjects block wrapped in the shared resiliency policy:
   `git grep -n "securityMigrationPolicy.Execute" -- Service/Source/Service.Configuration/Database/ManagementHistory/MhMigrationManager.cs`
7. **679970** new tests present:
   `git grep -n "ReadDatabaseParameters_NonCanonicalMultiSubnetFailover_FallsBackToNull" -- Service/Tests/Service.Configuration.Tests/Service/ARServiceHelperConfigurationProviderTests.cs`
   `git grep -n "CreateConnection_ManagedIdentity_TokenAcquisitionFailure_DisposesConnectionAndPropagates" -- Service/Tests/Service.Configuration.Tests/Database/ManagementHistory/DatabaseConfigManagedIdentityTests.cs`
   `git grep -n "MakeConnectionString_ManagedIdentity_ExplicitIndefiniteConnectTimeout_IsPreserved" -- Service/Tests/Service.Configuration.Tests/Database/ManagementHistory/DatabaseConfigManagedIdentityTests.cs`

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

## Test — MI regression suite, all green (MUST include the new tests)
Find the built `ActiveRoles.Service.Configuration.Tests.dll` and run the MI
classes with vstest.console.exe:
```
& "<vstest.console.exe>" <ActiveRoles.Service.Configuration.Tests.dll> `
  /TestCaseFilter:"FullyQualifiedName~ManagedIdentity|FullyQualifiedName~SqlHelpersSecurityType|FullyQualifiedName~ARServiceHelper|FullyQualifiedName~DatabaseConfig"
```
Require all green. In particular these NEW/CHANGED tests must pass:
- `ReadDatabaseParameters_NonCanonicalMultiSubnetFailover_FallsBackToNull`
- `MakeConnectionString_ManagedIdentity_ExplicitIndefiniteConnectTimeout_IsPreserved`
- `CreateConnection_ManagedIdentity_TokenAcquisitionFailure_DisposesConnectionAndPropagates`
- `MakeConnectionString_NoAdvancedProperties_LeavesBuilderDefaults` (now asserts
  60, not 15 — this is the intended 679972 unification; a FAIL here asserting 15
  means the source change did not land)
Record passed/failed/skipped totals and the status of each named test above.

## Report (REQUIRED — this is the task's only tracked change)
Write `verify-report-95956-threads-round2.md` at the repo root containing:
- the content-gate grep results (pass/fail each, with the match line/count)
- build: per-solution errors / warnings counts (and error lines if any)
- test: passed / failed / skipped totals + status of the 4 named tests + any
  failing test names
- any restore step you ran
- a final one-line verdict: `VERIFY PASS` or `VERIFY FAIL: <reason>`

Stage/commit that single file so it is captured. Do NOT touch any other file.

## Do NOT
Do not fetch ADO origin, do not edit source, do not push to any ADO branch. The
worker harness handles pushing your report to the throwaway task branch in the
hive mirror only.
