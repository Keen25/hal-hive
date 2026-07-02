# SPEC — L2 live DirSync smoke test against the lab DC (green-light proof)

You are mini-HAL on ar1 (domain-joined Windows, running as `fodor\a`).
Repo: `D:\src\ActiveRoles`. GOAL: prove the **725181** DirSync engine actually
tracks a real AD change end-to-end against the live lab DC — a genuine green
light, not a mock. This is a THROWAWAY verification harness: **do NOT push, do
NOT open a PR, do NOT commit to any existing feature branch.**

## Lab facts (verified)
- Domain / realm: `fodor.monster.local` (Kerberos realm `FODOR.MONSTER.LOCAL`).
- Live DC: `dc1.fodor.monster.local` = **192.168.100.10** (PDC, GC, KDC, writable).
- Root/partition DN: `DC=fodor,DC=monster,DC=local`.
- Credentials: user `a`, password `<LAB_PASS>` (you already run as `fodor\a`).
- Users container: `CN=Users,DC=fodor,DC=monster,DC=local`.

## Where to put it
Work on a fresh local branch off the pushed 725181 tip so the tracker code is
present and you don't disturb the spike. **Do NOT run `git fetch`** — the clone
has no cached credentials and a fetch will hang forever waiting for auth. The
remote-tracking ref `origin/feature/725181_oi-ldap-dirsync-change-tracker` is
already local and points at `0330f3a1c6` (verified). Just branch off it:
```
git checkout -B wip/l2-live-dirsync origin/feature/725181_oi-ldap-dirsync-change-tracker
```
If you ever think you need remote data, STOP and report instead of fetching.
Add ONE new test file:
`Service\Tests\Service.Tests\DirectorySynchronization\OiLdapDirSyncLiveTests.cs`
in namespace `ActiveRoles.Service.Tests.DirectorySynchronization`. The
`Service.Tests` project already references `Service.ActiveDirectory` and
`OneIdentity.Ldap`, and already contains `OiLdapDirSyncChangeTrackerTests` with a
managed `SearchResult.Creator` (`TestSearchResultCreator`/`TestSearchResult`).
Reuse that same creator pattern (copy a minimal managed creator into the new
file if the existing one is private) so `SearchResult.Create()` works. Apply the
C4 discipline: `[DoNotParallelize]`, save/restore `SearchResult.Creator` in
`[ClassInitialize]`/`[ClassCleanup]` (guard the restore against null).

## The real LdapClient (mirror `LdapIdentifierLookup.Create`)
```csharp
var options = new LdapClientOptions
{
    LdapHost   = "dc1.fodor.monster.local",   // if Kerberos SPN/DNS misbehaves, also try "192.168.100.10"
    LdapPort   = 389,
    AuthMethod = LdapAuthMethod.Kerberos,
    Username   = "a",
    Password   = "<LAB_PASS>",
    Domain     = "FODOR.MONSTER.LOCAL",        // realm; if bind fails, try null (RootDSE-discovered)
    Tls        = new LdapTlsOptions { UseSsl = false },
};
using var client = new LdapClient(options);
// If the API needs an explicit connect before DirSync, call it (ConnectAsync/Connect);
// otherwise the tracker's first poll will connect. Reflect the API if unsure.
```
Confirm exact option/member names against the rc.7 assembly if any differ
(you verified `ConnectedHost` via MetadataLoadContext before — same approach).
Report any deviation.

## The test (this is the green light)
`[TestMethod]` `LiveDirSync_DetectsNewUser_Incrementally` — steps:
1. `var tracker = new OiLdapDirSyncChangeTracker(client);`
2. `const string rootDn = "DC=fodor,DC=monster,DC=local";`
3. **Initial full sync:** `ChangeSet cs1 = await tracker.GetChangesAsync(rootDn, null);`
   - Assert `cs1.UpdatedCookie != null && cs1.UpdatedCookie.Length > 0`.
   - Assert `cs1.Changes.Count > 0` (a full sync of the lab domain returns objects).
4. **Make a real AD change** — create a unique throwaway user via
   `System.DirectoryServices` (works because ar1 is domain-joined):
   ```csharp
   string cn = "l2test-" + Guid.NewGuid().ToString("N").Substring(0, 8);
   using var users = new DirectoryEntry("LDAP://dc1.fodor.monster.local/CN=Users," + rootDn);
   var entry = users.Children.Add("CN=" + cn, "user");
   entry.Properties["sAMAccountName"].Value = cn;
   entry.CommitChanges();
   ```
   Wrap the whole test body in try/finally and in `finally` delete the user
   (`users.Children.Remove(entry)` / `entry.DeleteTree()`), swallowing cleanup errors.
5. **Incremental poll:** `ChangeSet cs2 = await tracker.GetChangesAsync(rootDn, cs1.UpdatedCookie);`
   - Assert some change in `cs2.Changes` corresponds to the new user: match by DN
     containing `"CN=" + cn` (case-insensitive), OR read its `objectGUID`
     attribute from the created entry and match on that. **This assertion passing
     is the green light.**
6. (Bonus, if easy) delete the user, poll again with `cs2.UpdatedCookie`, assert a
   change for the same object surfaces (tombstone/delete). Don't fail the run if
   only the bonus fails — report it separately.

## Build + run
Build via the solution (Debug|x64, `D:\VS2022\MSBuild...` as before), then run
ONLY this class:
```
vstest.console.exe <ActiveRoles.Service.Tests.dll> /Tests:LiveDirSync_DetectsNewUser_Incrementally
```

## Iterate on failure (you have autonomy here)
If the bind/DirSync fails, DIAGNOSE and retry within reason:
- Auth: try host as IP `192.168.100.10` vs FQDN; try `Domain=null`; check for a
  clock-skew / KDC error in the exception and report it verbatim.
- Rights: under the tracker's ObjectSecurity mode `fodor\a` needs only read on the
  partition; if you get a `DirSyncAccessDeniedException`, report it (that itself is
  a meaningful result about the account, not a code bug).
- Connectivity: `Test-NetConnection 192.168.100.10 -Port 389` if binds time out.
Do NOT weaken the assertion to force a pass. A red result with a clear reason is a
valid outcome — report it honestly.

## Report -> `D:\tmp\l2live_report.md`
- Final verdict: **GREEN** (incremental new-user detected) or **RED** (+ exact reason).
- The initial-sync change count and cookie length.
- The exact change entry that matched the new user (DN / objectGUID).
- Bonus delete-detection result if attempted.
- Any auth/connect adjustments you had to make (host form, realm, connect call).
- The full content of `OiLdapDirSyncLiveTests.cs`.
- Confirm the test user was cleaned up.
Do NOT push, do NOT PR. Leave `wip/l2-live-dirsync` local.
