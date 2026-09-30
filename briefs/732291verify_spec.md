# SPEC — Build + test verify of PBI 732291 ConfigCenter MI SQL token injection

You are mini-HAL on ar1. This is a VERIFY task. Your clone is checked out at base
branch `feature/732291_configcenter-mi-wiring` (full ARS source tree; this branch is
stacked on 732334, so it also contains the 732334 persisted-client-id key). **Do NOT
fetch from ADO origin; do NOT modify any tracked source.** The only file you may
create is the report at the end.

## Content sanity gate (do this FIRST; if any check fails, STOP and report)
Confirm the 732291 change is present in the checked-out tree:
1. `git grep -n "class ConfigManagedIdentitySql" -- Service/Source/Service.Configuration/Database/ConfigManagedIdentitySql.cs` — must match (the public facade).
2. `git grep -n "Service.Configuration.csproj" -- ConfigCenter/Configuration.UI/Configuration.UI.csproj` — must match (the new ProjectReference).
3. `git grep -n "ConfigManagedIdentitySql.ApplyAccessToken" -- ConfigCenter/Configuration.UI/InPlaceUpgrade/UpgradeHelper.cs` — must match (>=2 hits: GetConnectionDetails + CheckDbIsUpdated).
4. `git grep -n "ConfigManagedIdentitySql.ApplyAccessToken" -- ConfigCenter/Configuration.UI/Tabs/AdministrationService/ImportDatabase/ValidateAddOnDatabaseHelper.cs` — must match.
5. `git grep -n "SecurityTypes.ManagedIdentity" -- ConfigCenter/Configuration.UI/InPlaceUpgrade/UpgradeHelper.cs` — must match (>=2: GetConnectionDetails + GetSQLConnectionBuilder).
6. `git grep -n "class ConfigManagedIdentitySqlTests" -- Service/Tests/Service.Configuration.Tests/Database/ConfigManagedIdentitySqlTests.cs` — must match.
7. `git grep -n "class UpgradeHelperManagedIdentityTests" -- ConfigCenter/Tests/Configuration.UI.Tests/InPlaceUpgrade/UpgradeHelperManagedIdentityTests.cs` — must match.
Confirm ALSO that the Phase-1 throw is GONE:
8. `git grep -n "deferred to Phase 2" -- ConfigCenter/Configuration.UI/Tabs/AdministrationService/ImportDatabase/ValidateAddOnDatabaseHelper.cs` — must return NOTHING (0 hits).
If checks 1-7 match and check 8 is empty, proceed.

## Build (Debug|x64) — TWO solutions
Resolve VS2022 MSBuild (via `vswhere` or the known `D:\VS2022\MSBuild\Current\Bin\MSBuild.exe`).
Cold-tree prep if needed (same as prior ARS tasks): run `/t:Restore` first, then
`nuget.exe restore <sln> -PackagesDirectory External` if `project.assets.json` /
`BuildTools.CodeSigner` is missing. Transient `AL1078`/`LNK1346` strong-name file-lock
races under parallel build — retry the build a few times.

### Build 1 — ConfigCenter solution (proves the new cross-solution ProjectReference + UI wiring compile)
```
& "<MSBuild.exe>" ConfigCenter\ActiveRoles.ConfigCenter.sln /p:Configuration=Debug /p:Platform=x64 /m /v:m
```
Require **0 errors**. This build MUST exercise the new
`Configuration.UI -> Service.Configuration` ProjectReference; a missing/circular
reference will fail here. Record error + warning counts; capture exact
`error CS####` / `error MSB####` lines on failure.

### Build 2 — Service solution (builds the facade test project)
```
& "<MSBuild.exe>" Service\ActiveRoles.Service.sln /p:Configuration=Debug /p:Platform=x64 /m /v:m
```
Require **0 errors**. Record error + warning counts.

## Test — TWO test DLLs
Resolve `vstest.console.exe` (same VS2022 install).

### Test A — ConfigCenter UI wiring tests
Find the built `ActiveRoles.Configuration.UI.Tests.dll` (under
`ConfigCenter\out\x64\Debug\` or the test project's `bin\x64\Debug\net48\`) and run:
```
& "<vstest.console.exe>" <ActiveRoles.Configuration.UI.Tests.dll> `
  /TestCaseFilter:"FullyQualifiedName~UpgradeHelperManagedIdentityTests"
```
Require all green. Confirm these ran + passed:
- `GetSQLConnectionBuilder_ConfigDb_ManagedIdentity_EmitsManagedIdentityShape`
- `GetSQLConnectionBuilder_ManagementHistoryDb_ManagedIdentity_EmitsManagedIdentityShape`
- `GetSQLConnectionBuilder_ConfigDb_WindowsIntegrated_IsNotManagedIdentityShape`

### Test B — Service.Configuration facade tests
Find the built `ActiveRoles.Service.Configuration.Tests.dll` (under
`Service\out\x64\Debug\`) and run:
```
& "<vstest.console.exe>" <ActiveRoles.Service.Configuration.Tests.dll> `
  /TestCaseFilter:"FullyQualifiedName~ConfigManagedIdentitySqlTests"
```
Require all green. Confirm these ran + passed:
- `ApplyAccessToken_ManagedIdentityShapedString_SetsAccessTokenFromProvider`
- `ApplyAccessToken_NonManagedIdentityString_LeavesAccessTokenUnsetAndDoesNotCallProvider`
- `ApplyAccessToken_NullConnection_DoesNotThrow`
- `CreateConnection_ManagedIdentityBuilder_InjectsTokenAndLeavesClosed`
- `CreateConnection_NonManagedIdentityBuilder_ReturnsUnopenedWithoutToken`
- `NormalizeClientId_NullOrEmpty_ReturnsNullForSystemAssigned`
- `NormalizeClientId_Guid_ReturnsCanonicalGuid`
- `NormalizeClientId_NonGuid_Throws`

## Report (REQUIRED — this is the task's only tracked change)
Write a file `verify-report-732291.md` at the repo root containing:
- the content-gate grep results (pass/fail each of the 8 checks)
- build 1 (ConfigCenter) + build 2 (Service): errors / warnings counts (and error lines if any)
- test A + test B: passed / failed / skipped totals + explicit pass/fail for each named test above
- any restore/retry step you ran
- a final one-line verdict: `VERIFY PASS` or `VERIFY FAIL: <reason>`
Then stage/commit that single file so it is captured. Do NOT touch any other file.

## Do NOT
Do not fetch ADO origin, do not edit source, do not push to any ADO branch. The
worker harness handles pushing your report to the throwaway task branch in the
hive mirror only.
