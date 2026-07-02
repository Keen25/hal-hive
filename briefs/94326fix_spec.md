# SPEC — PR #94326 spike review fixes (Adam's 5 findings)

You are mini-HAL on ar1. Repo: `D:\src\ActiveRoles`, branch already
`feature/711235_ndj-runtime-composition-spike` @ `2d72007525` (rebased on the
fixed 725181 base). Do NOT push (master-HAL force-pushes). Do NOT run `git fetch`
(no creds → hangs). Implement all 5 findings, build the full solution (0 errors),
run `NdjRuntimeCompositionTests` + any new tests (all green), then commit.

Reviewer = Adam Fodor, PR #94326, "Approved with suggestions". Findings:

---

## F1 (Medium) — Lease/connection ownership: nothing disposes the lease
`DirectorySynchronizerFactory.CreateForActiveDirectory` does
`var lease = ClientConnections.DirSyncPool.Acquire();` then passes only
`lease.Client` to `DirectorySynchronizer.CreateForOiLdapTransport(...)` and drops
the lease. `Lease.Dispose()` (the only path that disposes the client) is never
called. Not a live leak today (`Acquire()`→`DefaultLdapClientFactory` throws
first), but must be fixed before live wiring.

**Fix — make `DirectorySynchronizer` own the lease and dispose it.**
`DirectorySynchronizer` is already `IDisposable` (it disposes `_cts`). Extend it to
optionally own one `IDisposable` resource (the lease):
- Add a field `private IDisposable _ownedResource;`.
- In `Dispose()`, dispose it too: `_ownedResource?.Dispose();` (alongside `_cts`).
- Add an optional param to the OI.Ldap factory method:
  `public static DirectorySynchronizer CreateForOiLdapTransport(string rootDN,
  OneIdentity.Ldap.ILdapClient ldapClient, DirSyncRequestProcessor requestProcessor,
  IDisposable ownedResource = null)` — set `_ownedResource = ownedResource` on the
  instance before returning it. (This method lives in `DirectorySynchronizer.cs`,
  from 725181 — editing it on the spike branch is fine.)
- In the factory, pass the lease as the owned resource:
  `return DirectorySynchronizer.CreateForOiLdapTransport(rootDN, lease.Client, requestProcessor, ownedResource: lease);`
- Update the factory's inline comment (currently "lease lifetime follows the
  synchronizer; real return/reuse is 711235 follow-up") to state the ownership is
  now real: the synchronizer disposes the lease on teardown; real pool return/reuse
  is still 711235 follow-up.
`ILdapConnectionLease : IDisposable` already, so passing it as `IDisposable` works.

## F4 (Low) — TOCTOU on static `DirSyncPool`
Same factory reads `ClientConnections.DirSyncPool` for the availability check, then
dereferences `ClientConnections.DirSyncPool.Acquire()` again; `Shutdown()` may null
it in between → NRE.
**Fix:** capture one local snapshot and use it for both:
```csharp
var pool = ActiveRoles.Service.ClientConnections.DirSyncPool;
bool poolAvailable = pool != null;
var transport = DirSyncTransportSelector.Select(ActiveRoles.Service.ClientConnections.IsNonDomainJoined, poolAvailable);
if (transport == DirSyncTransport.OiLdap)
{
    var lease = pool.Acquire();
    ...
}
```

## F3 (Medium) — Startup composition (`ClientConnections.Initialize` NDJ mode + pools) untested
Core PR behavior — deriving mode from `NdjMode.IsEnabled()` and building the two
pools — has NO test. `Initialize()` also does heavy process-identity work that may
be unsafe to call in a unit test host.
**Fix — extract the composition into a testable seam and test it.**
- In `ClientConnections`, extract the mode+pool composition currently at the top of
  `Initialize(...)` into an `internal static` method, e.g.:
  ```csharp
  internal static void ApplyNdjComposition(System.Func<OneIdentity.Ldap.ILdapClient> ldapClientFactory)
  {
      IdentityMode = NdjMode.IsEnabled() ? IdentityProviderMode.Ldap : IdentityProviderMode.Windows;
      if (IdentityMode == IdentityProviderMode.Ldap)
      {
          var factory = ldapClientFactory ?? DefaultLdapClientFactory;
          RequestPool = new StubLdapConnectionPool(factory);
          DirSyncPool = new StubLdapConnectionPool(factory);
      }
  }
  ```
  and have `Initialize(...)` call `ApplyNdjComposition(ldapClientFactory);` in place
  of that inline block (behavior unchanged). Also extract pool teardown into an
  `internal static void DisposePools()` (dispose+null both) called from `Shutdown()`.
- Ensure `Service` has `[InternalsVisibleTo("ActiveRoles.Service.Tests")]` (add it if
  missing — check `Service`'s AssemblyInfo/csproj; the `PublicKey` form if the others
  use strong-named IVT).
- Add tests (new class `ClientConnectionsCompositionTests`, `[DoNotParallelize]`) that
  toggle NDJ via the **`ARS_NDJ_MODE` env var** (that's what `NdjMode.IsEnabled()`
  reads) and inject a fake factory so no `NotImplementedException`:
  - NDJ on: `Environment.SetEnvironmentVariable("ARS_NDJ_MODE","1")`, call
    `ApplyNdjComposition(() => new Mock<ILdapClient>().Object)` → assert
    `DirSyncPool != null && RequestPool != null` and `IsNonDomainJoined` true.
  - NDJ off: env var cleared → `ApplyNdjComposition(null)` → both pools null,
    `IsNonDomainJoined` false.
  - `DisposePools()` (or `Shutdown` path) → both pools null again.
  - Restore the env var and call `DisposePools()` in `[TestCleanup]`/`finally`.
  Confirm `NdjMode.IsEnabled()` truly keys on `ARS_NDJ_MODE` (grep it); if it also
  requires the file be ABSENT, ensure the test host has no `ndj-endpoints.json`.

## F2 (Medium) — Factory documented as tested, but isn't
No test references `DirectorySynchronizerFactory`/`CreateForActiveDirectory`.
**Fix — add a real test of the OI.Ldap composition branch** (in
`NdjRuntimeCompositionTests` or the new class). Using the F3 setup (NDJ env + fake
factory so `ClientConnections.DirSyncPool` is a real `StubLdapConnectionPool`):
- Build a `DirSyncRequestProcessor` to satisfy `CreateForActiveDirectory`. Check its
  constructor: if it's mockable via Moq (`new Mock<DirSyncRequestProcessor>(...).Object`)
  or has an accessible ctor, use that; if it is genuinely not constructible in a test,
  refactor `DirectorySynchronizerFactory` minimally so the branch is testable (e.g. an
  `internal` overload that accepts the pool + a `Func` and returns the transport
  decision / the lease), and test that. Prefer the least-invasive option that actually
  asserts: **(a)** with NDJ+pool the OI.Ldap branch is taken and the pool's `Acquire()`
  is called, and **(b)** disposing the returned `DirectorySynchronizer` disposes the
  lease/client exactly once (ties to F1 — use a `Mock<ILdapClient>().As<IDisposable>()`
  and `Verify(Dispose, Times.Once())`).
- Correct the factory's class-level comment: it should say the OI.Ldap composition
  branch is now covered by unit tests (not "exercised by unit tests only" while it
  isn't), and that live-loop wiring remains 725428 follow-up.

## F5 (Low) — `DirectoryDiscovery` evaluates NDJ independently of the cached seam
`DirectoryDiscovery.CreateCurrent()` calls `NdjMode.IsEnabled()` itself; `_current` is
`static readonly` (runs once), a *second* independent evaluation from
`ClientConnections`'s cached `IdentityMode`. If `ndj-endpoints.json` toggles between
the two evaluations, discovery and identity/pool subsystems disagree.
**Fix (prefer routing through the single seam; else document):**
- First choice: route `DirectoryDiscovery` through the cached
  `ClientConnections.IsNonDomainJoined`, and make `_current` a `Lazy<IDirectoryDiscovery>`
  so it's computed on first use (after `Initialize()` in real startup order). Add an
  XML-doc note that `DirectoryDiscovery` must not be consulted before
  `ClientConnections.Initialize()`.
- BUT first verify no code path uses `DirectoryDiscovery` before `Initialize()` runs
  (grep usages of `DirectoryDiscovery.Current`/`.Instance`). If such an early consumer
  exists, do NOT reorder — instead keep the independent `NdjMode.IsEnabled()` eval and
  just add a clear XML-doc comment documenting that both read the same `NdjMode` source
  and the narrow toggle-window caveat. Report which option you chose and why.

---

## Build + test
Full solution, Debug|x64 (VS2022 at `D:\VS2022`, via `vswhere` if needed). Require 0
errors. Run `NdjRuntimeCompositionTests` + your new test class(es):
```
vstest.console.exe <ActiveRoles.Service.Tests.dll> /Tests:NdjRuntimeComposition,ClientConnectionsComposition
```
All green. Also re-run `OiLdapDirSyncChangeTrackerTests` once to confirm no regression.

## Commit (do NOT push)
```
git add -A
git commit -m "711235 - Address PR #94326 spike review: lease ownership, composition tests, TOCTOU, discovery seam

- F1: DirectorySynchronizer now owns+disposes the pool lease (IDisposable ownedResource on CreateForOiLdapTransport)
- F4: capture a local DirSyncPool snapshot in the factory (TOCTOU vs Shutdown)
- F3: extract ApplyNdjComposition/DisposePools testable seam; add startup-composition tests (NDJ pools built vs null; Shutdown nulls)
- F2: add a real OI.Ldap-branch factory test (Acquire taken, lease disposed once); correct the factory coverage comment
- F5: <route DirectoryDiscovery through ClientConnections.IsNonDomainJoined | documented init-order> (report which)

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

## Report -> `D:\tmp\94326fix_report.md`
Build errors/warnings, test totals (list new test names), the commit SHA, F5 choice
+ rationale, DirSyncRequestProcessor constructibility outcome (F2), whether you added
`InternalsVisibleTo`, and anything you couldn't do. Do NOT push.
