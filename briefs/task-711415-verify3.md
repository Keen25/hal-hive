Verify a build/test only — DO NOT modify, commit, or push any code.

SOURCE (use the swarm's internal mirror — you do NOT have ADO Code-scope git creds):
- Clone/fetch from: http://192.168.100.1:8418/ActiveRoles.git
  Authenticate with the HIVE_TOKEN bearer (the same way you enumerate mirror refs;
  e.g. git -c http.extraHeader="Authorization: Bearer $HIVE_TOKEN").
- Branch: feature/711415_jwt-first-class-caller
- Commit: 6878320db6 (must be the checked-out HEAD). Confirm HEAD == 6878320db6.
  This branch/commit is now present in the mirror (just pushed).

DURABLE LOG (results must survive %TEMP% cleanup):
- Ensure C:\hive-logs exists.
- Tee ALL restore/build/test console output into C:\hive-logs\task-711415-verify3.log
  (Tee-Object or redirect). State the absolute path in your final summary and confirm it exists.

Steps:
1. Fetch the mirror, checkout feature/711415_jwt-first-class-caller @ 6878320db6, confirm HEAD.
2. Restore + build the affected net48 projects with msbuild (Release or repo default):
   - Service/Source/Service/Service.csproj
   - Service/Source/Service.Api/Service.Api.csproj
   - Service/Tests/Service.Api.Tests/Service.Api.Tests.csproj
   - Service/Tests/Service.Tests/Service.Tests.csproj
   (Build the enclosing solution if that is how the repo builds.)
   NuGet restore: use the JIT packaging token / NuGet.Config already provisioned to you.
3. Run the unit tests (vstest.console/dotnet test as the repo uses):
   - ClaimsBackedIdentityTests (Service.Api.Tests) — includes 8 GUID-validation tests:
     non-GUID oid/tid, conflicting dual-form oid/tid, same-GUID-different-format,
     non-canonical-canonicalized-in-Name (also asserts raw PrincipalId), and the two NEW
     zero-GUID tests (Create_ZeroObjectId_ReturnsAuthenticationError,
     Create_ZeroTenantId_ReturnsAuthenticationError) — plus existing ones.
   - ClaimsCallerIdentityTest (Service.Tests).
4. Report concisely:
   - build result (success/fail) per project; if fail, exact compiler errors (file:line, code, msg).
   - per-suite total / passed / failed / skipped counts.
   - any failing test: name + assertion/exception message.
   - the durable log path.

If you STILL cannot obtain the source from the mirror, STOP and report the exact auth/fetch error
(command + stderr) — do not improvise other sources.

Context: 6878320db6 sits on dfbe606ec1; adds Guid.Empty rejection in ResolveGuidClaim (+2 mirror
tests) and extends the non-canonical test to assert ClientConnection.PrincipalId exposes the raw
braced/upper OID. LangVersion=latest. Goal: confirm net48 build compiles and all
ClaimsBackedIdentityTests + ClaimsCallerIdentityTest pass. Verification only.
