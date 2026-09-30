BUILD + UNIT-TEST VALIDATION ONLY — read-only. Do NOT modify, create, or commit any source files. Report results only (a single report file at the end is the only allowed write).

Context: Compile + unit-test gate for the PR !96673 review-follow-up HARDENING commit on the Active Roles tenant-import page. These edits were authored on a Linux box with NO MSBuild, so they have NOT been compiled or test-run. You are the first real build gate.

FIRST, guard against a stale checkout: run `git log -1 --oneline` at the repo root and CONFIRM HEAD is ccc58bfed5 ("732291 - Fail closed when the selected tenant vanishes on reconnect-on-Next (PR !96673 review)"), parent b36e8d086d. If HEAD is any other commit, STOP and report the wrong SHA — do not build a stale checkout.

Content sanity gate (do this FIRST; if any check fails, STOP and report):
1. `git grep -n "_userSelectedTenantId" -- ConfigCenter/Configuration.UI/Tabs/AdministrationService/ImportDatabase/Pages/ValidateTenantPage.cs` — must match (>=4 hits: field decl + remember-block + FirstOrDefault match + comments).
2. `git grep -n "selectedTenantVanished" -- ConfigCenter/Configuration.UI/Tabs/AdministrationService/ImportDatabase/Pages/ValidateTenantPage.cs` — must match (>=3: decl + set + guard).
3. `git grep -n "ValidateTenant_SelectedTenantNoLongerAvailable" -- ConfigCenter/Configuration.UI/Strings.resx ConfigCenter/Configuration.UI/Strings.de-DE.resx ConfigCenter/Configuration.UI/Strings.Designer.cs` — must match in ALL THREE files.
4. `git grep -n "SelectedIndex" -- ConfigCenter/Configuration.UI/Tabs/AdministrationService/ImportDatabase/ValidateTenantView.xaml` — must return NOTHING (0 hits; the SelectedIndex="0" was removed).
If checks 1-3 match and check 4 is empty, proceed.

Files touched by the commit under validation:
- ConfigCenter/Configuration.UI/Tabs/AdministrationService/ImportDatabase/Pages/ValidateTenantPage.cs  (page-level _userSelectedTenantId Guid + fail-closed vanished-tenant guard)
- ConfigCenter/Configuration.UI/Tabs/AdministrationService/ImportDatabase/ValidateTenantView.xaml       (dropped SelectedIndex="0" on cb_Tenants)
- ConfigCenter/Configuration.UI/Strings.resx / Strings.de-DE.resx / Strings.Designer.cs                  (new localized string ValidateTenant_SelectedTenantNoLongerAvailable)

Build (Debug|x64):
Resolve VS2022 MSBuild (via `vswhere` or the known `D:\VS2022\MSBuild\Current\Bin\MSBuild.exe`).
Cold-tree prep if needed (same as prior ARS tasks): run `/t:Restore` first, then
`nuget.exe restore <sln> -PackagesDirectory External` if `project.assets.json` / `BuildTools.CodeSigner` is missing.
Transient `AL1078`/`LNK1346` strong-name file-lock races under parallel build — retry the build a few times.

Build — ConfigCenter solution (proves the tenant-page C#, the XAML, and the new Strings resource compile):
```
& "<MSBuild.exe>" ConfigCenter\ActiveRoles.ConfigCenter.sln /p:Configuration=Debug /p:Platform=x64 /m /v:m
```
Require **0 errors**. Watch specifically for (new/changed surfaces):
- CS errors in ValidateTenantPage.cs around `_userSelectedTenantId`, `new AzureTenantConnectionData()`, `viewModel.MultipleTenants?.FirstOrDefault(...)`, `viewModel.AddError("", Strings.ValidateTenant_SelectedTenantNoLongerAvailable)`.
- A missing-resource error (MSB3103 / CS0117 on Strings) would mean the new key did not round-trip through resx -> Designer.
- XAML/XamlParseException at build time from the ComboBox change.
Record error + warning counts; capture exact `error CS####` / `error MSB####` lines on failure.

Test — ConfigCenter UI tests (the tenant-page + MI import coverage):
Resolve `vstest.console.exe` (same VS2022 install). Find the built `ActiveRoles.Configuration.UI.Tests.dll`
(under `ConfigCenter\out\x64\Debug\` or the test project's `bin\x64\Debug\net48\`) and run the two relevant classes:
```
& "<vstest.console.exe>" <ActiveRoles.Configuration.UI.Tests.dll> `
  /TestCaseFilter:"FullyQualifiedName~ValidateTenantViewModelTests|FullyQualifiedName~ValidateAddOnDatabaseHelperManagedIdentityTests"
```
Require all green. These are pure VM / helper tests (no WPF host, SQL, or IMDS). Record passed / failed / skipped totals and list any failed test names + assertion messages. NOTE: there is intentionally NO test that drives ValidateData's vanished-tenant path (it needs a full WPF/PropertySheet host that the test project does not stand up) — its absence is expected, not a failure.

Report (REQUIRED — the only allowed tracked change): write `verify-report-96673.md` at the repo root containing:
- HEAD SHA confirmed (ccc58bfed5 yes/no)
- content-gate results (pass/fail each of the 4 checks)
- build: errors / warnings counts (and error lines if any)
- tests: passed / failed / skipped totals + explicit pass/fail per test in the two named classes
- any restore/retry step you ran
- a final one-line verdict: `VERIFY PASS` or `VERIFY FAIL: <reason>`
Then stage/commit that single file so it is captured. Do NOT touch any other file.

Do NOT: fetch ADO origin, edit source, or push to any ADO branch. The worker harness handles pushing your report to the throwaway task branch in the hive mirror only.
