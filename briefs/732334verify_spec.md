# SPEC — Build + test verify of PBI 732334 MI client-id persist/reload

You are mini-HAL on ar1. This is a VERIFY task. Your clone is checked out at base
branch `feature/732334_mi-persist-reload` (full ARS source tree + the 732334
config-side persist/reload change). **Do NOT fetch from ADO origin; do NOT modify
any tracked source.** The only file you may create is the report at the end.

## Content sanity gate (do this FIRST; if any check fails, STOP and report)
Confirm the 732334 change is present in the checked-out tree:
1. `git grep -n "ManagedIdentityClientId = \"ManagedIdentityClientId\"" -- Common/Source/Configuration.Common/ConfigKeys.cs` — must match.
2. `git grep -n "PersistManagedIdentityClientId" -- Service/Source/Service.Configuration/Database/ArsOleDbConnectionString.cs` — must match (2 call sites + 1 method).
3. `git grep -n "ConfigKeys.Database.ManagedIdentityClientId" -- Service/Source/Service.Configuration/Service/ARServiceHelper.cs` — must match.
4. `git grep -n "PutToRegistryEx_ManagedIdentity_UserAssigned_PersistsClientId" -- Service/Tests/Service.Configuration.Tests/Database/ArsOleDbConnectionStringManagedIdentityTests.cs` — must match.
5. `git grep -n "RegisterThenRead_ManagedIdentity_UserAssigned_RoundTripsClientId" -- Service/Tests/Service.Configuration.Tests/Service/ARServiceHelperConfigurationProviderTests.cs` — must match.
If all five match, proceed.

## Build (Debug|x64)
Resolve VS2022 MSBuild (via `vswhere` or the known `D:\VS2022\MSBuild\Current\Bin\MSBuild.exe`
used in prior ARS tasks) and build the Service solution from the repo root of THIS clone:
```
& "<MSBuild.exe>" Service\ActiveRoles.Service.sln /p:Configuration=Debug /p:Platform=x64 /m /v:m
```
Require **0 errors**. Record error + warning counts. On failure, capture the exact
`error CS####` / `error MSB####` lines. Cold-tree prep if needed (same as prior ARS
tasks): run `/t:Restore` first, then `nuget.exe restore Service\ActiveRoles.Service.sln
-PackagesDirectory External` if `BuildTools.CodeSigner` / `project.assets.json` is
missing. Transient `AL1078`/`LNK1346` strong-name file-lock races under parallel
build — retry the build a few times.

## Test
Find the built `ActiveRoles.Service.Configuration.Tests.dll` (under
`Service\out\x64\Debug\`) and run these two classes with vstest.console.exe:
```
& "<vstest.console.exe>" <ActiveRoles.Service.Configuration.Tests.dll> `
  /TestCaseFilter:"FullyQualifiedName~ArsOleDbConnectionStringManagedIdentityTests|FullyQualifiedName~ARServiceHelperConfigurationProviderTests"
```
Require all green. Explicitly confirm these NEW tests ran and passed:
- `PutToRegistryEx_ManagedIdentity_UserAssigned_PersistsClientId`
- `PutToRegistryEx_ManagedIdentity_SystemAssigned_ClearsStaleClientId`
- `PutToRegistry_ManagedIdentity_UserAssigned_PersistsClientId`
- `PutToRegistryEx_SqlServerAuthentication_DoesNotWriteClientId`
- `ReadDatabaseParameters_ManagedIdentity_UserAssigned_ReadsClientIdIntoAccountName`
- `ReadDatabaseParameters_ManagedIdentity_SystemAssigned_ReturnsEmptyAccountName`
- `RegisterThenRead_ManagedIdentity_UserAssigned_RoundTripsClientId`
Also confirm the PRE-EXISTING MI tests in `ArsOleDbConnectionStringManagedIdentityTests`
still pass (e.g. `PutToRegistryEx_ManagedIdentity_DoesNotWriteCredentials`,
`PutToRegistryEx_ManagedIdentity_NullCredentials_DoesNotThrow`,
`PutToRegistry_ManagedIdentity_DoesNotWriteLogin`).

## Report (REQUIRED — this is the task's only tracked change)
Write a file `verify-report-732334.md` at the repo root containing:
- the five content-gate grep results (pass/fail each)
- build: errors / warnings counts (and error lines if any)
- test: passed / failed / skipped totals + explicit pass/fail for each named test above
- any restore/retry step you ran
- a final one-line verdict: `VERIFY PASS` or `VERIFY FAIL: <reason>`
Then stage/commit that single file so it is captured. Do NOT touch any other file.

## Do NOT
Do not fetch ADO origin, do not edit source, do not push to any ADO branch. The
worker harness handles pushing your report to the throwaway task branch in the
hive mirror only.
