BUILD + UNIT-TEST VALIDATION ONLY — read-only. Do NOT modify, create, or commit any source files. Report results only (a single report file at the end is the only allowed write).

Context: Compile + unit-test gate for Task 736079 — extraction of the `CarryForwardTenant` seam out of `ValidateTenantPage.ValidateData` plus 6 new unit tests. Authored on a Linux box with NO MSBuild, so NOT yet compiled or test-run. You are the first real build gate. Stacked on the PR !96673 fix commit ccc58bfed5.

FIRST, guard against a stale checkout: run `git log -1 --oneline` at the repo root and CONFIRM HEAD is 15bfd5fcdc ("736079 - Extract CarryForwardTenant seam and unit-test vanished-tenant / second-Next path"), parent ccc58bfed5. If HEAD is any other commit, STOP and report the wrong SHA — do not build a stale checkout.

Content sanity gate (do this FIRST; if any check fails, STOP and report):
1. `git grep -n "internal enum TenantCarryResult" -- ConfigCenter/Configuration.UI/Tabs/AdministrationService/ImportDatabase/Pages/ValidateTenantPage.cs` — must match (1 hit: the extracted enum).
2. `git grep -n "internal static TenantCarryResult CarryForwardTenant" -- ConfigCenter/Configuration.UI/Tabs/AdministrationService/ImportDatabase/Pages/ValidateTenantPage.cs` — must match (1 hit: the extracted method).
3. `git grep -n "CarryForwardTenant(priorViewModel, viewModel, ref _userSelectedTenantId)" -- ConfigCenter/Configuration.UI/Tabs/AdministrationService/ImportDatabase/Pages/ValidateTenantPage.cs` — must match (1 hit: the ValidateData call site).
4. `git grep -n "selectedTenantVanished" -- ConfigCenter/Configuration.UI/Tabs/AdministrationService/ImportDatabase/Pages/ValidateTenantPage.cs` — must return NOTHING (0 hits; the old local bool flag was replaced by the enum result). The fail-closed guard now reads `if (tenantCarry == TenantCarryResult.Vanished)`.
5. New test file exists: `git grep -c "\[TestMethod\]" -- ConfigCenter/Tests/Configuration.UI.Tests/ImportDatabase/ValidateTenantPageCarryForwardTests.cs` — must report 9.
If checks 1-3 and 5 match and check 4 is empty, proceed.

Files touched by the commit under validation:
- ConfigCenter/Configuration.UI/Tabs/AdministrationService/ImportDatabase/Pages/ValidateTenantPage.cs  (extracted internal enum TenantCarryResult + internal static CarryForwardTenant; ValidateData now calls it and maps Vanished -> AddError + return false; behavior-preserving)
- ConfigCenter/Tests/Configuration.UI.Tests/ImportDatabase/ValidateTenantPageCarryForwardTests.cs        (NEW; 6 tests for the seam)

Build (Debug|x64):
Resolve VS2022 MSBuild (via `vswhere` or the known `D:\VS2022\MSBuild\Current\Bin\MSBuild.exe`).
Cold-tree prep if needed (same as prior ARS tasks): run `/t:Restore` first, then
`nuget.exe restore <sln> -PackagesDirectory External` if `project.assets.json` / `BuildTools.CodeSigner` is missing.
Transient `AL1078`/`LNK1346` strong-name file-lock races under parallel build — retry the build a few times.

Build — ConfigCenter solution (proves the refactored page C# AND the new test file compile):
```
& "<MSBuild.exe>" ConfigCenter\ActiveRoles.ConfigCenter.sln /p:Configuration=Debug /p:Platform=x64 /m /v:m
```
Require **0 errors**. Watch specifically for (new/changed surfaces):
- CS errors in ValidateTenantPage.cs around the new `TenantCarryResult` enum, `CarryForwardTenant(...)` signature (`ref Guid`), `FirstOrDefault`, `new AzureTenantConnectionData()`.
- CS errors in the new test file: it references the `internal` `ValidateTenantPage.CarryForwardTenant` and `TenantCarryResult` via the existing `InternalsVisibleTo("ActiveRoles.Configuration.UI.Tests")` grant — a CS0122 (inaccessible) here would mean the IVT wiring broke.
Record error + warning counts; capture exact `error CS####` / `error MSB####` lines on failure.

Test — ConfigCenter UI tests:
Resolve `vstest.console.exe` (same VS2022 install). Find the built `ActiveRoles.Configuration.UI.Tests.dll`
(under `ConfigCenter\out\AnyCPU\Debug\` or the test project's `bin\...\net48\`) and run the relevant classes:
```
& "<vstest.console.exe>" <ActiveRoles.Configuration.UI.Tests.dll> `
  /TestCaseFilter:"FullyQualifiedName~ValidateTenantPageCarryForwardTests|FullyQualifiedName~ValidateTenantViewModelTests|FullyQualifiedName~ValidateAddOnDatabaseHelperManagedIdentityTests"
```
Expected totals: 9 (new ValidateTenantPageCarryForwardTests) + 6 (ValidateTenantViewModelTests) + 27 (ValidateAddOnDatabaseHelperManagedIdentityTests) = 42, all passed, 0 failed, 0 skipped. Record passed / failed / skipped and list any failed test names + assertion messages. The 9 new tests are pure VM/seam logic (no WPF host, SQL, or IMDS).

Report (REQUIRED — the only allowed tracked change): write `verify-report-736079.md` at the repo root containing:
- HEAD SHA confirmed (15bfd5fcdc yes/no)
- content-gate results (pass/fail each of the 5 checks)
- build: errors / warnings counts (and error lines if any)
- tests: passed / failed / skipped totals + explicit per-test pass/fail for the 6 ValidateTenantPageCarryForwardTests, plus totals for the other two classes
- any restore/retry step you ran
- a final one-line verdict: `VERIFY PASS` or `VERIFY FAIL: <reason>`
Then stage/commit that single file so it is captured. Do NOT touch any other file.

Do NOT: fetch ADO origin, edit source, or push to any ADO branch. The worker harness handles pushing your report to the throwaway task branch in the hive mirror only.
