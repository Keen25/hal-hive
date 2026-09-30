# Task: complete WI 734592 — DispSpec SqlBulkCopy Managed-Identity wiring regression tests (PR !96978)

You are a One Identity Active Roles engineer working on branch
`feature/734592_dispspec-sqlbulkcopy-mi-test` (already checked out for you from the
central mirror). Target ADO PR: !96978 in `Active Roles/ActiveRoles`. This is a
**legacy net48** codebase. You MUST build and run the affected tests green before
you push. Do NOT weaken or delete any existing passing assertion to go green.

## Background (the production change these tests guard)

`Service/Source/Service.Database/Globalization/DispSpecResourceLoader.cs` method
`Save2Db(string dispSpecXML)` was fixed for Managed Identity (MI). The OLD code was
`new SqlBulkCopy(ArsDatabase.ConnectionString, SqlBulkCopyOptions.TableLock)` — a bare
connection string that SqlBulkCopy uses to build its OWN internal SqlConnection, which
never receives the MI access token → fails under MI. The FIX opens a token-injected
connection and hands it to the SqlConnection overload:

```csharp
using (SqlConnection connection = ArsDatabase.GetConnectionConf())
using (SqlBulkCopy bulkCopy = new SqlBulkCopy(connection, SqlBulkCopyOptions.TableLock, null))
{ ... bulkCopy.WriteToServer(tab); }
```

`GetConnectionConf()` is the MI-aware path (applies the access token). The same fix was
applied at a second site: `DispSpecLocale.SaveCollection`. The `Save2Db` catch block was
also expanded so a failed bulk copy (e.g. MI auth failure) — which leaves the language
node childless because `ClearARSDisplaySpecifiersNode()` and `SaveRootLanguageNode()`
already ran — raises a startup-visible event:

```csharp
catch (Exception ex)
{
    Trace.TraceError("[...Save2Db] Unable to save display specifiers ... {0}", ex);
    try
    {
        StringBuilder builder = new StringBuilder();
        builder.AppendLine(String.Format(ErrorStrings.ErrorLoadingDisplaySpecifiersNC, _culture.Name));
        DispSpecLoader.AppendAllMessagesFromException(builder, ex);
        Server.EventLog.WriteEvent(ServerEvents.NonCriticalErrorOnStartup,
            ErrorReportingHelper.GetWarningInfo(ServerEvents.NonCriticalErrorOnStartup.InstanceId, 6, ex),
            ClientConnections.ServerConnection.PrincipalId,
            builder.ToString());
    }
    catch (Exception eventLogEx) { Trace.TraceError("[...Save2Db] Failed to write startup event log entry ... {0}", eventLogEx); }
}
```

Do NOT modify this production code (it is already on the branch / merged from develop),
EXCEPT the small null-guard called out in Deliverable 6 below — read that carefully, it
is fenced and optional-pending-your-judgement.

## The five deliverables (this is the agreed acceptance contract — land ALL of them)

### Deliverable 1 — IL ctor guard + negative fixture (EXISTS; HARDEN it)
File: `Service/Tests/Service.Tests/DispSpecSqlBulkCopyManagedIdentityWiringTests.cs`
(no DB; an IL-disassembly guard that reads `MethodBase.GetMethodBody().GetILAsByteArray()`
and walks ECMA-335 bytecode to confirm `Save2Db` / `SaveCollection` construct
`SqlBulkCopy(SqlConnection,...)` and never `SqlBulkCopy(string,...)`).

Apply these hardening changes (from PR review threads 688860/688861/688862/688865):

1. **688860 — widen the resolve `catch` but KEEP FALL-THROUGH (do NOT use `continue`).**
   In `ReadReferencedMethods`, the token-resolve `try` currently catches only
   `ArgumentException`. Widen it to also tolerate loader/image failures via an exception
   filter, and let control FALL THROUGH to the `pos += OperandSize(...)` advance that sits
   AFTER the try/catch. **A `continue` here is a BUG** — it skips the operand-size advance
   and desyncs the IL walk (the metadata-token bytes get read as opcodes). Add
   `using System.IO;` for the IO exception types.
   ```csharp
   catch (Exception ex) when (
       ex is ArgumentException ||
       ex is BadImageFormatException ||
       ex is FileLoadException ||
       ex is FileNotFoundException ||
       ex is TypeLoadException)
   {
       // Tolerated: token that cannot be resolved in this test's load context.
       // MUST fall through to the operand advance below — a `continue` would desync
       // the walk (the advance sits after this try/catch). A genuinely missing
       // SqlBulkCopy ctor still reds the final Assert.IsTrue below (fails closed).
   }
   ```

2. **688861 — add `BindingFlags.DeclaredOnly`** to the reflection lookup that finds the
   two target methods (`FindSingleMethod` / equivalent), so an inherited overload can never
   masquerade as the target. Both targets (`DispSpecResourceLoader.Save2Db`,
   `DispSpecLocale.SaveCollection`) are private instance methods — verify and keep it that way.

3. **688862 — add an orientation comment** at the `typeof(SqlBulkCopy)` / `typeof(SqlConnection)`
   type-identity checks noting these bind `System.Data.SqlClient` by identity; a future
   migration to `Microsoft.Data.SqlClient` would make the guard fail with a misleading
   "no SqlBulkCopy construction" — the fix then is to repoint the `typeof` targets, not to
   assume the wiring broke. (Comment only.)

4. **688865 — add an orientation `<remarks>`** on the IL-walker method (`ReadReferencedMethods`)
   stating: this is the only IL-disassembling test in the repo; when it reds, the FIRST
   hypothesis should be a benign refactor (e.g. the bulk copy was extracted into a helper),
   in which case repoint the guard at the new method — do NOT "fix" the walker. Keep the
   walker itself unchanged. Do NOT promote it to Service.Test.Common (YAGNI until a 2nd consumer).

5. **CRITICAL NEW HARDENING — the guard is shape, not dataflow (deep-review finding #1).**
   The current guard sets independent booleans (`usesConnectionOverload`,
   `callsGetConnectionConf`, `!constructsRawConnection`, `!usesConnectionStringOverload`) but
   NEVER verifies the `SqlConnection` argument passed to `new SqlBulkCopy(conn,...)` is the
   RETURN VALUE of `GetConnectionConf()`. A regression such as
   `new SqlBulkCopy(someOtherConnection, ...)` or `new SqlBulkCopy(connection.Clone(), ...)`
   would pass every current boolean while breaking MI. **Close this hole**: extend the IL
   walker to track a minimal operand stack (or the local-slot dataflow) across the
   `call GetConnectionConf` → `newobj SqlBulkCopy(SqlConnection,...)` sequence and assert the
   connection consumed by the ctor is the value produced by `GetConnectionConf()` (i.e. the
   `GetConnectionConf` result is stored to a local via the `using` and that same local is
   loaded immediately before the `newobj`). If a full stack model is too costly, at minimum
   assert there is NO intervening `callvirt SqlConnection.Clone()` / no other `SqlConnection`
   producer between the `GetConnectionConf` call and the `SqlBulkCopy` `newobj`, and document
   the residual gap in the method `<remarks>`. Add a focused negative unit test that would go
   RED for the `Clone()` / foreign-connection regression.

### Deliverable 2 — wiring + disposal (STATIC IL; be HONEST about what it proves)
Prove `SqlConnection` and `SqlBulkCopy` are each disposed. **The current walker records only
call/callvirt/newobj target methods; it has ZERO exception-handling-clause awareness, so
counting `Dispose` callvirts == 2 is GAMEABLE** (deep-review finding #2): a regression to bare
sequential `connection.Dispose(); bulkCopy.Dispose();` with no try/finally — the exact
leak-on-throw bug this guards — yields the same count and would PASS. Do the honest thing:

- Extend the IL analysis to read `MethodBody.ExceptionHandlingClauses` (available via
  reflection on net48 assemblies), filter to clauses where
  `Flags == ExceptionHandlingClauseOptions.Finally`, and assert that each
  `IDisposable.Dispose` call site for the `SqlConnection` and `SqlBulkCopy` locals falls
  WITHIN a `Finally` handler region (correlate the Dispose callvirt IL offset against each
  clause's `HandlerOffset`/`HandlerLength`). This requires the walker to retain IL offsets.
- If, after attempting it, exact ownership correlation proves infeasible in the time budget,
  the fallback is: assert "exactly one Dispose call site each for SqlConnection and SqlBulkCopy,
  BOTH inside Finally-flagged handler regions" (existence + in-finally, even if not per-local
  ownership) and state the residual limitation in the test name and `<remarks>`. Do NOT name a
  test or write an assert message claiming "disposed exactly once on both paths" unless the
  assertion actually backs that claim. Naming must match provable scope.

### Deliverable 3 — failure branch (LIVE-DB runtime test; the seam EXISTS)
File: `Service/Tests/Service.DatabaseTests/CodeTests/DispSpecResourceLoaderTest.cs`.
Force `Save2Db`'s `WriteToServer` to throw and assert the catch's startup event fires.

Confirmed feasible (deep-review finding #3):
- **Event capture seam:** swap a capturing writer into the process-global via the generated
  accessor `Quest_ActiveRolesServer_Service_ArsEventLogAccessor.Writer`
  (`Service/Tests/Service.Test.Common/VSCodeGenAccessors.cs`; it does
  `PrivateType.SetStaticField("_writer", value)` on `ArsEventLog`). Save the previous writer,
  install your capturing `EventLogWriter` subclass, RESTORE in `finally`. Copy the proven
  pattern verbatim from
  `Service/Tests/Service.Tests/DirectorySynchronization/DirectorySynchronizerPoisonQuarantineTests.cs`
  (the `CapturingPrincipalEventLogWriter` swap/restore, ~lines 300-326). Override the exact
  overload `WriteEvent(EventInstance, byte[], string, params object[])` and record the args.
  Mark the test class/method `[DoNotParallelize]` and restore the writer in `finally` (the
  writer is process-global; leaking it corrupts sibling tests).
- **Fault injection that reaches WriteToServer specifically** (NOT an earlier statement):
  `Save2Db` runs `ClearARSDisplaySpecifiersNode()` → `SaveRootLanguageNode()` → build DataTable
  → `WriteToServer`. Feed a test XML fixture with **two `<ds:Class>` nodes that share the same
  `className`** → identical computed `distinguishedName` → violates the unique constraint
  `UQ_EDMDispSpec_distinguishedName` (see `Service/Source/Service.Configuration/Database/Scripts/tables.sql`)
  during `WriteToServer`. (Alternative lever: an oversized `classDisplayName` > 255 chars —
  the column is `utDisplayName = nvarchar(255)`.) A malformed-XML fixture is NOT acceptable
  here — it throws in `doc.LoadXml` BEFORE the nested `using`s are entered, so it cannot
  exercise Deliverable 2's disposal-on-throw path. Put a comment pointing at the DDL so the
  coupling (the test stops throwing if the constraint/width changes in a future migration) is
  visible to future maintainers.
- **Assertions:** the captured call is `WriteEvent(instance, data, principalId, values)`. Assert
  `instance == ServerEvents.NonCriticalErrorOnStartup`; the message (`values[0]`, the
  `builder.ToString()`) CONTAINS the formatted `ErrorStrings.ErrorLoadingDisplaySpecifiersNC`
  text for `_culture.Name` AND the inner-exception chain (unique-constraint SqlException
  message). The **index `6` lives inside the opaque `data` byte[]** produced by
  `ErrorReportingHelper.GetWarningInfo(...InstanceId, 6, ex)` — do NOT try to decode the byte[]
  at runtime. Verify index 6 EITHER by asserting the byte[] equals a freshly computed
  `GetWarningInfo(NonCriticalErrorOnStartup.InstanceId, 6, ex)` for the captured exception, OR
  document that index==6 is pinned by the static-IL guard in Deliverable 3b. Assert NOTHING
  escapes `Save2Db` (the method returns normally — the outer `catch(Exception)` swallows).

### Deliverable 3b — static IL pin of the catch shape (adds the index-6 guarantee)
In the Service.Tests IL guard, add an assertion over `Save2Db`'s catch region that the body
references `ErrorStrings.ErrorLoadingDisplaySpecifiersNC`, `ServerEvents.NonCriticalErrorOnStartup`,
`ErrorReportingHelper.GetWarningInfo`, and `Server.EventLog.WriteEvent`, AND that the constant
`6` is the operand feeding `GetWarningInfo`. The current walker does NOT decode `ldc.i4*`
operands and does not scope to the exception-handler region — extend it to (a) recognize the
`ldc.i4.0..8` / `ldc.i4.s` / `ldc.i4` opcode family (for the short forms the value is baked into
the opcode byte itself; `6` emits the single-byte `ldc.i4.6` with NO trailing operand), and
(b) restrict this assertion to the IL offset range of `Save2Db`'s `Finally`/`Catch` clause so a
`6` elsewhere cannot satisfy it. This is what makes "index 6" non-tautological (it reds if the
index or the branch changes). If (b) proves infeasible in budget, assert the `ldc.i4.6`
immediately precedes the `GetWarningInfo` call token and say so in `<remarks>`.

### Deliverable 4 — fix Save2DbTest asserts + a MEANINGFUL positive post-condition
File: `Service/Tests/Service.DatabaseTests/CodeTests/DispSpecResourceLoaderTest.cs`,
`Save2DbTest`.

- The three `Assert.AreEqual(obj.ToString(), "0")` calls have **reversed MSTest args**
  (`AreEqual(expected, actual)`); flip to `Assert.AreEqual("0", obj.ToString(), "<msg>")`.
  **Be honest in the commit/PR text: this is DIAGNOSTIC-ONLY.** The three
  `EDMDispSpecLoading1..3` queries are each `if exists(<exact correct row>) select 0 else
  select 1`; on a skipped/reverted bulk copy the rows are absent → query returns `1` → the
  existing equality already FAILS. So the flip changes only the failure message, not pass/fail.
  (The relayed claim "== '0' passes if the copy is skipped" is FALSE for these queries — do
  not repeat it.)
- Add a positive cardinality post-condition. Use an **exact count**, not `> 0`:
  `select count(*) from EDMDispSpec where name like '%-Display'` and assert it equals **3**
  (the `DispSpecTest.xml` fixture defines exactly class1/class2/class3, and child rows are named
  `<className>-Display`; the root language node is named as the LCID hex e.g. `409`, so it does
  NOT match `%-Display` and does not inflate the count). A `> 0` check is defeated by leftover
  rows from other fixtures and adds nothing over Loading1..3 — use `== 3`.
- Add the query as a NEW test-local resx string (e.g. `EDMDispSpecChildCount`) in
  `Service/Tests/Service.DatabaseTests/SQLQueries.resx`. **You MUST regenerate
  `SQLQueries.Designer.cs` in the SAME commit** (add the matching `internal static string
  EDMDispSpecChildCount` accessor) or the build fails with CS0117. Match the exact codegen shape
  of the sibling accessors. Alternatively, if resx+Designer regen is fragile in your toolchain,
  inline the SQL as a `const string` in the test — but prefer the resx to match repo convention.

### Deliverable 5 — second site (SaveCollection) (already covered by D1's guard)
Ensure the IL guard asserts `DispSpecLocale.SaveCollection` with the SAME token-injected-connection
and disposal checks as `Save2Db`. If the shipped guard already covers it, just confirm and extend
the D1.5 dataflow + D2 in-finally checks to this second method too.

### Deliverable 6 — (FENCED, use judgement) production null-guard in Save2Db catch
Deep review flagged that `ClientConnections.ServerConnection.PrincipalId` in the `Save2Db` catch
is dereferenced without a null guard, while the sibling call at `ClientConnections.cs` (the
`ServerConnection != null ? ServerConnection.PrincipalId : string.Empty` idiom, ~line 658) guards
it. In the Service.DatabaseTests harness `ClientConnections.Initialize()` runs (TestEnvironment.cs)
so `ServerConnection` is non-null and your Deliverable-3 test will pass WITHOUT this change.
**Therefore: do NOT change production code as part of this test PR.** Instead, if your build/run
confirms the deref is reachable-when-null in any startup path, leave a single PR comment noting it
as a separate bug for its own work item. Only apply the null-guard to production if the user (via
the PR) explicitly asks — otherwise this stays a note, not a code change.

### Deliverable 7 — PR hygiene (thread 688617)
- Add an explicit `<ProjectReference Include="..\..\Source\Service.Database\Service.Database.csproj" />`
  to `Service/Tests/Service.Tests/Service.Tests.csproj`. It currently resolves Service.Database only
  TRANSITIVELY (via Service.Cache/Service.Test.Common/Service.CorePolicies/Service.Workflow); the
  explicit ref is safe (build DAG stays acyclic) and stabilizes future refactors.
- Do NOT edit the PR description from the worker (the human owns that). Instead, write a short
  `CHANGES-734592.md` at repo root (or update the PR body via the promote step) summarizing: three
  test methods (two guards + one negative fixture) now become the hardened set; "first CI compile"
  caveat is obsolete; item-4 flip is diagnostic-only; the dataflow + in-finally + failure-branch
  hardening added. Keep it factual.

## Build & verify gate (MANDATORY before push)
1. Build the solution (net48, msbuild). Fix any compile error you introduced.
2. Run `Service.Tests` (the IL guard + negative fixtures) — MUST be green.
3. Run `Service.DatabaseTests` `DispSpecResourceLoaderTest` (Save2DbTest + the new failure-branch
   test) against the live test DB the harness provisions — MUST be green. If the DB harness is
   unavailable on your lane, STOP and report that Service.DatabaseTests could not be executed;
   do NOT push unverified DB tests.
4. Quick mutation sanity (cheap, do if time allows): temporarily revert the MI ctor to
   `new SqlBulkCopy(string,...)` and confirm the D1 guard REDS; temporarily change index `6`→`5`
   and confirm D3b REDS; restore. This proves the guards are non-tautological. Report the result.

## Commit / branch conventions
- Commit message format: `734592 - <imperative summary>`.
- Do NOT force-push. Push the feature branch. The change lands centrally in the mirror and is
  promoted to ADO PR !96978 by `halctl promote` (HAL side), so just push clean commits to
  `feature/734592_dispspec-sqlbulkcopy-mi-test`.
- Report back: which deliverables landed fully, which fell back (and to what honest form), the
  build result, and both test-run results (green/failed with the tail of any failure).

If any deliverable proves infeasible on inspection, DO NOT fake it or weaken an assertion to pass —
report the blocker with the file:line evidence and the honest minimal form you shipped instead.

---

## RUNTIME DB TARGET for Service.DatabaseTests (Deliverables 3 & 4) — DO NOT COMMIT

`Service.DatabaseTests` provisions/drops real databases on a SQL Server. On this
worker, point it at **sql1** using SQL auth **SA / Passw0rd**.

The harness (`Service/Tests/Service.DatabaseTests/TestEnvironment.cs:75`) reads the
XPath node `//DBVersion_Current/ConnectionStrings/WithWindowsAuthentication` from
`Service/Tests/Service.DatabaseTests/Configuration/ArsDatabaseTestConfiguration.xml`.
`DispSpecResourceLoaderTest` uses `TestEnvironment.DbCurrent` (that `DBVersion_Current`
node), so ONLY that node must be retargeted for D3/D4.

Before running the DB tests, set the `DBVersion_Current` → `WithWindowsAuthentication`
node to:
```xml
<WithWindowsAuthentication
    Server="sql1"
    BasicDbName="for-unit-tests"
    AuthenticationMode="0"
    Login="SA"
    Password="Passw0rd"
/>
```
(`AuthenticationMode="0"` = SQL auth; `Login`/`Password` are only read when mode=0.
The node name stays `WithWindowsAuthentication` because the XPath is hard-coded to it —
the `AuthenticationMode` attribute, not the element name, selects auth type.)

**This edit is RUNTIME-ONLY. It is NOT a deliverable.**
- Do **NOT** `git add` / commit `ArsDatabaseTestConfiguration.xml`.
- The PR diff must not contain any Server/Login/Password change.
- After the DB tests pass, run `git checkout -- Service/Tests/Service.DatabaseTests/Configuration/ArsDatabaseTestConfiguration.xml`
  (or `git restore` it) so the working tree is clean before you stage the real deliverables.
- Verify with `git status` that this file is NOT staged and NOT modified in the final commit.

If sql1 is unreachable or the DB harness cannot provision (permissions, SMO, etc.),
do NOT silently skip D3/D4 — build+run everything else, then report D3/D4 as blocked
with the exact failure so it can be retried, rather than marking the task green.
