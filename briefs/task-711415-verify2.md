Verify a build/test only — DO NOT modify, commit, or push any code.

Repo: ActiveRoles (Azure DevOps, dev.azure.com/1id, Active Roles project).
Branch: feature/711415_jwt-first-class-caller
Commit: 6878320db6 (must be the checked-out HEAD).

DURABLE LOG (do this so results survive %TEMP% cleanup):
- Create directory C:\hive-logs if missing.
- Tee ALL restore/build/test console output to C:\hive-logs\task-711415-verify2.log
  (e.g. run msbuild/vstest with output redirected/`Tee-Object` into that file).
- In your final summary, state the absolute path and confirm the file exists.

Steps:
1. Clone/fetch the ActiveRoles repo and checkout branch feature/711415_jwt-first-class-caller
   at commit 6878320db6. Confirm HEAD == 6878320db6.
2. Restore + build the affected net48 projects with msbuild (Release or the repo default):
   - Service/Source/Service/Service.csproj
   - Service/Source/Service.Api/Service.Api.csproj
   - Service/Tests/Service.Api.Tests/Service.Api.Tests.csproj
   - Service/Tests/Service.Tests/Service.Tests.csproj
   (Build the enclosing solution if that is how the repo builds; these are the projects touched.)
3. Run the unit tests (vstest.console/dotnet test as the repo uses), specifically:
   - ClaimsBackedIdentityTests (Service.Api.Tests) — this now includes 8 GUID-validation
     tests: non-GUID oid/tid, conflicting dual-form oid/tid, same-GUID-different-format,
     non-canonical canonicalized-in-Name (now also asserting raw PrincipalId), and the two
     NEW zero-GUID tests (Create_ZeroObjectId_ReturnsAuthenticationError,
     Create_ZeroTenantId_ReturnsAuthenticationError) — plus the existing ones.
   - ClaimsCallerIdentityTest (Service.Tests).
4. Report back, concisely:
   - build result (success/fail) per project; if fail, exact compiler errors (file:line, code, message).
   - per-suite total / passed / failed / skipped counts.
   - for any failing test: test name + assertion/exception message.
   - the durable log path (C:\hive-logs\task-711415-verify2.log).

Context: commit 6878320db6 sits on top of dfbe606ec1. It adds a Guid.Empty rejection in
ResolveGuidClaim (all-zero oid/tid -> Invalid) with 2 mirror tests, and extends the
non-canonical test to assert ClientConnection.PrincipalId still exposes the raw braced/upper
OID (canonical Name vs raw PrincipalId split). LangVersion=latest. Goal: confirm the net48
build compiles and all ClaimsBackedIdentityTests + ClaimsCallerIdentityTest pass. No code
changes — verification only.
