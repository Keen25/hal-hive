# SPEC — PR #94311 review fixes (PBI 725181)

You are mini-HAL on ar1. Repo: `D:\src\ActiveRoles`. Branch is already
`feature/725181_oi-ldap-dirsync-change-tracker` (do NOT create a new branch,
do NOT push — master-HAL reviews + pushes). Implement all fixes below, build,
run tests, then commit locally.

Two files carry the production code; one carries tests:
- `Service\Source\Service.ActiveDirectory\DirectorySynchronization\OiLdapDirSyncChangeTracker.cs`
- `Service\Source\Service.ActiveDirectory\DirectorySynchronization\DirectorySynchronizer.cs`
- `Service\Tests\Service.Tests\DirectorySynchronization\OiLdapDirSyncChangeTrackerTests.cs`

Review verdict was REQUEST CHANGES with 4 Medium blockers (C1–C4) + lower
items (C5, C6, C7, C8). Fix ALL of them. Details, with exact current code:

---

## C1 — broad `InvalidOperationException` catch → silent full resync
`OiLdapDirSyncChangeTracker.GetChangesAsync`, the `catch (InvalidOperationException ex)
when (checkpoint != null && checkpoint.Host != null)` at ~L66. The filter is
true for EVERY resumed poll, so a transient `EnsureConnected()` disconnect
(also an `InvalidOperationException` from inside `DirSyncAllClassifiedAsync`) is
misclassified as a DC host-guard trip → the valid cookie is discarded and a
full partition resync is issued, and the real error is swallowed.

**Fix:** replace the filter with a real host comparison. Only treat it as a
host-guard mismatch (and reset to full sync) when the persisted
`checkpoint.Host` and the live `_client.ConnectedHost` are BOTH non-null and
DIFFER (use the `NormalizeHost` helper from C7 for the comparison). In every
other case let the `InvalidOperationException` propagate. Concretely add:

```csharp
private bool IsHostGuardMismatch(DirSyncCheckpoint checkpoint)
{
    string persisted = NormalizeHost(checkpoint?.Host);
    string connected = NormalizeHost(_client.ConnectedHost);
    return persisted != null && connected != null &&
           !string.Equals(persisted, connected, StringComparison.OrdinalIgnoreCase);
}
```
and change the catch to `catch (InvalidOperationException ex) when (IsHostGuardMismatch(checkpoint))`.
Confirm `ILdapClient` exposes `ConnectedHost` (it does per rc.7 decompile). If
the member name differs, use the actual property that returns the connected DC
host and note it in your report.

## C7 — host guard is exact-string (IP vs FQDN mismatch → resync each reconnect)
Add a `NormalizeHost` helper used by C1's comparison:

```csharp
// Normalises a DirSync checkpoint host for DC-affinity comparison. Trims and
// lowercases. NOTE (documented limitation): an IP literal and the DC's FQDN are
// NOT reconciled here — reconnecting to the same DC via a different host-string
// form (e.g. NDJ connect-by-IP vs a persisted FQDN) still counts as a mismatch
// and forces one full resync. Persisting _client.ConnectedHost verbatim (as the
// codec already does) keeps both sides consistent within a stable transport.
private static string NormalizeHost(string host)
    => string.IsNullOrWhiteSpace(host) ? null : host.Trim().ToLowerInvariant();
```
Keep the comment — it IS the "documented decision" the reviewer asked for.

## C2 — malformed/foreign cookie decoded outside `try` → partition wedged forever
`DecodeCheckpoint(cookie)` runs at ~L59 BEFORE the `try`, and throws
`InvalidDataException`/`EndOfStreamException` on a corrupt, truncated, or legacy
MSDS DirSync cookie. `DirectorySynchronizer.Synchronize()` has no try/catch and
only advances the cookie after success → the poison cookie is re-fed forever, no
self-heal. Triggers: a 1–3 byte cookie hits `ReadInt32()` before the length
guard; a legacy 0x44-byte MSDS cookie (first bytes little-endian `0x5344534D`)
is read as a huge host length.

**Fix — make `DecodeCheckpoint` total (never throws on bad input; returns null =
full resync), and add a version/magic prefix so adapter cookies are self-identifying:**
1. Add a 4-byte magic/version prefix. Define
   `private static readonly byte[] CookieMagic = { 0x41, 0x52, 0x53, 0x31 }; // "ARS1"`.
2. `EncodeCheckpoint`: write `CookieMagic` first, then the existing
   `[int32 hostLen][host?][cookie]` payload.
3. `DecodeCheckpoint`: 
   - null/empty → return null (initial sync, unchanged).
   - If `cookie.Length < 4` OR the first 4 bytes != `CookieMagic` → `Trace.TraceWarning`
     ("unrecognised/legacy DirSync cookie, forcing full resync") and return null.
   - Then read the rest defensively; any `IOException`/`EndOfStreamException`/
     `InvalidDataException`/`ArgumentException` → TraceWarning + return null.
   - Keep the existing host-length sanity guard.
   Do NOT let any exception escape `DecodeCheckpoint`.
Because the adapter is new/inert there are no persisted production cookies, so the
format change is safe (an old-format cookie simply forces one full resync).

Also (defence in depth) in `GetChangesAsync`, keep the decode where it is but it
can no longer throw. No cookie should ever wedge the partition again.

## C3 (+ C6) — replication-rights diagnostic is semantically inverted; flags opaque
`SyncFlags = DirSyncFlags.Default | DirSyncFlags.AncestorsFirstOrder` (~L38);
`DirSyncFlags.Default = ObjectSecurity | IncrementalValues`. So `ObjectSecurity`
(LDAP_DIRSYNC_OBJECT_SECURITY, least-privilege) IS set — the DC returns only
readable objects/attributes and does NOT require `DS-Replication-Get-Changes`.
Under ObjectSecurity an `InsufficientAccessRights` means missing ordinary
read/list access, NOT missing replication rights. The current
`DirSyncReplicationRightsException` message + the comment at ~L108-112 + the PR
description are therefore inverted and misdirect operators.

**Fix:**
- **C6:** make the flags explicit and self-documenting:
  `private const DirSyncFlags SyncFlags = DirSyncFlags.ObjectSecurity | DirSyncFlags.IncrementalValues | DirSyncFlags.AncestorsFirstOrder;`
  (keep the existing explanatory comment; update it to say ObjectSecurity =
  least-privilege mode.)
- Rename `DirSyncReplicationRightsException` → `DirSyncAccessDeniedException`
  (update all references incl. tests). Reword its message to something like:
  `"DirSync change tracking on partition \"{baseDn}\" was denied. Under
  ObjectSecurity (least-privilege) mode the bound account needs read/list access
  to the monitored partition; if ObjectSecurity is later removed,
  DS-Replication-Get-Changes replication rights are required instead. Grant the
  appropriate access and retry."`
- Update the comment block at ~L108-112 to match (remove the "requires
  DS-Replication-Get-Changes / no least-privilege fallback" claim; ObjectSecurity
  IS the least-privilege path).

## C4 — test mutates process-global `SearchResult.Creator`, no cleanup / parallel guard
`OiLdapDirSyncChangeTrackerTests`: `[ClassInitialize]` sets the process-wide
`SearchResult.Creator` and there is no `[ClassCleanup]`, no `[DoNotParallelize]`.
MSTest v3 parallelises test classes → race/leak with any other class calling
`SearchResult.Create()`.

**Fix:**
- Add `[DoNotParallelize]` on the `OiLdapDirSyncChangeTrackerTests` class.
- Capture the previous creator in `[ClassInitialize]`
  (`_previousCreator = SearchResult.Creator;`) and restore it in a new
  `[ClassCleanup] public static void ClassCleanup()` (`SearchResult.Creator = _previousCreator;`).

## C5 — `DirectorySynchronizer.Synchronize()` blocks with a DEFAULT CancellationToken
`Synchronize()` (~L276) calls `_changeTracker.GetChangesAsync(_rootDN, _dirSyncCookie).GetAwaiter().GetResult()`
with no token, so a running poll can't be promptly cancelled on shutdown.

**Fix (minimal, non-breaking):**
- Add `private readonly CancellationTokenSource _cts = new CancellationTokenSource();` field.
- Pass `_cts.Token` into `GetChangesAsync(_rootDN, _dirSyncCookie, _cts.Token)`.
- Add `public void Stop() => _cts.Cancel();` (and make the class `IDisposable`
  disposing `_cts`, OR dispose `_cts` in an existing teardown if one exists —
  check the class; if none, implement `IDisposable`).
- In `Synchronize()`, allow `OperationCanceledException` to surface cleanly (don't
  wrap it). Do not change behavior when not cancelled. The default ctor path and
  existing `DirSyncChangeTracker` must still compile/behave identically.

## C8 — no rename/move test; downstream NRE risk
`DirectorySynchronizer.ConstructRenameMoveObjectRequest` (~L139) reads
`searchResult[AttributeNames.distinguishedName].StringValue`; a Move change lacking
`distinguishedName` NREs. The tracker must surface `distinguishedName` for a Move
entry (parity with legacy). Add a unit test in the tracker test class:
- A `DirSyncChange` of type `Move` whose `Entry` includes `distinguishedName`
  (and `name`, `objectGUID`) → `GetChangesAsync` yields a `SearchResult` that
  `Contains(distinguishedName)` with the expected value and correct `DN`.
Do NOT change `ConstructRenameMoveObjectRequest` behavior (legacy parity); the
test documents the required attribute presence.

---

## New tests to add (Janos's coverage gaps + above)
In `OiLdapDirSyncChangeTrackerTests`:
1. **Malformed cookie → full resync, no throw (C2):** feed
   `GetChangesAsync(BaseDn, new byte[]{1,2,3})` (and a fake legacy cookie:
   16 bytes starting `0x4D,0x53,0x44,0x53`) — assert the poll delegate is invoked
   with `checkpoint == null` (full sync) and NO exception is thrown.
2. **Non-host-guard IOE propagates (C1):** set up the mock so
   `ConnectedHost == "dc1"`, persisted checkpoint host == "dc1" (match), and the
   poll throws `InvalidOperationException` — assert `GetChangesAsync` rethrows the
   `InvalidOperationException` (does NOT resync). Add a companion asserting the
   genuine mismatch path still resyncs (ConnectedHost "dc2" vs persisted "dc1").
   Use `mock.SetupGet(c => c.ConnectedHost).Returns(...)`.
3. **Rename/Move mapping (C8):** as described in C8 above.
4. **Codec magic round-trip:** existing `Codec_RoundTrips_*` tests must still
   pass with the new magic prefix (they call Encode→Decode, so they will — verify).

Update the existing `InsufficientAccessRights`/`UnauthorizedAccess` tests to the
renamed `DirSyncAccessDeniedException`.

---

## Build + test (MUST pass before commit)
Full solution build, Debug|x64:
```
& "C:\Program Files\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe" `
  Service\ActiveRoles.Service.sln /p:Configuration=Debug /p:Platform=x64 /m /v:m
```
(If the MSBuild path differs, use the one you used for the 711235 spike.)
Run the tracker tests:
```
& "C:\Program Files\Microsoft Visual Studio\2022\Professional\Common7\IDE\CommonExtensions\Microsoft\TestWindow\vstest.console.exe" `
  <path-to-Service.Tests.dll> /Tests:OiLdapDirSyncChangeTrackerTests
```
Require: build 0 errors; ALL OiLdapDirSyncChangeTrackerTests pass (old + new).

## Commit (do NOT push)
```
git add -A
git commit -m "725181 - Address PR #94311 review: cookie/host-guard robustness, rights diagnostic, test isolation

- C1: narrow host-guard catch to a real ConnectedHost vs checkpoint.Host mismatch; let other IOEs propagate
- C2: make DecodeCheckpoint total (magic/version prefix, length guards) - malformed/legacy cookie -> full resync, never wedges the partition
- C3/C6: explicit DirSync flags; rename+reword DirSyncAccessDeniedException (ObjectSecurity is least-privilege, not repl-rights)
- C4: [DoNotParallelize] + save/restore SearchResult.Creator
- C5: plumb CancellationToken through DirectorySynchronizer.Synchronize()/Stop()
- C7: NormalizeHost with documented IP-vs-FQDN limitation
- C8 + coverage: malformed-cookie, non-host-guard-IOE, and rename/move tests

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

## Report
Write `D:\tmp\94311fix_report.md` with: build result (errors/warnings), test
results (count pass/fail, list any new test names), the commit SHA, any place
where the actual API/member names differed from this spec (esp. `ConnectedHost`),
and anything you could not do. Do NOT push.
