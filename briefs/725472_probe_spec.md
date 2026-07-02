# SPEC — #725472 live bind PROBE (force DefaultLdapClientFactory to fire on ar1)

mini-HAL on ar1. Repo `D:\src\ActiveRoles`, branch `feature/711235_ndj-runtime-composition-spike`.
The 9.0 overlay is already deployed to `C:\Program Files\One Identity\Active Roles\8.5\Service`
and the config DB `ActiveRoles850` is now stamped MajorDBVersion=850 (version gate satisfied).
Managed domain **fodor.monster.local** has override account **fodor.monster.local\rsvc-ar**
(edsaUseOverrideAccount=True). ARAdminSvc runs as FODOR\rsvc-ar.

## Goal
`DefaultLdapClientFactory` is lazy and its only caller (`DirectorySynchronizerFactory.
CreateForActiveDirectory`) is unwired until #725428, so a passive start never binds. Add a
TEMPORARY, env-gated startup PROBE that acquires ONE lease from `ClientConnections.RequestPool`
so the full path runs live: `DefaultLdapClientFactory` -> `LoadManagedDomainOverrideParameters`
(config DB, decrypted override creds) -> `DnsSrvDirectoryDiscovery.FindDomainControllerName`
-> `NdjLdapBinder.BindWithSslFallback` -> `ILdapClient.ConnectAndBind`. Capture the outcome to a
FILE (headless event-log/ArsTrace are blind on this host).

## Probe implementation (TEMPORARY — do NOT commit to the branch/PR)
Add the probe in the service startup sequence in `StartStop.cs`, placed **AFTER the service is
fully initialized and listeners are up** (i.e. at the END of the successful startup path, not
immediately at line 304) so that `Server.ProcessRequest` (used by `LoadManagedDomainOverride
Parameters`) is fully functional when the probe reads the config DB. Guard it so it only runs
when explicitly asked:

```csharp
// TEMP #725472 live-bind probe — DO NOT MERGE. Remove after capturing the bind result.
try
{
    if (ActiveRoles.Common.Services.ActiveDirectory.Configuration.NdjMode.IsEnabled()
        && string.Equals(System.Environment.GetEnvironmentVariable("ARS_NDJ_BIND_PROBE"),
                         "1", System.StringComparison.Ordinal))
    {
        System.IO.Directory.CreateDirectory(@"C:\tmp");
        System.IO.File.AppendAllText(@"C:\tmp\ndj_probe.log",
            $"{System.DateTime.UtcNow:o} PROBE START: acquiring from ClientConnections.RequestPool\n");
        try
        {
            using (var lease = ClientConnections.RequestPool.Acquire())
            {
                // Inspect whatever the lease exposes (the bound ILdapClient). Confirm the exact
                // member name from IArsLdapConnectionLease (e.g. lease.Client / lease.Connection).
                var client = lease /* .Client — use the real property */;
                System.IO.File.AppendAllText(@"C:\tmp\ndj_probe.log",
                    $"{System.DateTime.UtcNow:o} PROBE BIND OK: lease acquired, client type={client?.GetType().FullName}\n");
            }
        }
        catch (System.Exception exProbe)
        {
            System.IO.File.AppendAllText(@"C:\tmp\ndj_probe.log",
                $"{System.DateTime.UtcNow:o} PROBE BIND FAILED:\n{exProbe}\n");
        }
    }
}
catch { /* probe must never affect service startup */ }
```

- Confirm the real `IArsLdapConnectionLease` member that exposes the bound `ILdapClient` and use
  it (the snippet's `lease` placeholder must become e.g. `lease.Client`). If the lease exposes the
  client, log its type + (if cheap) whether it reports connected/bound.
- Use the correct `using` for `ClientConnections` / `NdjMode` (match existing usings in StartStop.cs).
- Do NOT gate on anything else; ARS_NDJ_MODE=1 is already set (NdjMode.IsEnabled()==true).

## Build (targeted)
Rebuild just the Service project to produce a fresh `ActiveRoles.Service.dll`:
`MSBuild Service\Source\Service\<the Service .csproj> /p:Configuration=Debug /p:Platform=x64 /m`
(or the minimal project that emits ActiveRoles.Service.dll). 0 errors. If a targeted build is
awkward, build the Service solution. Confirm the new dll timestamp.

## Deploy + run
1. `[Environment]::SetEnvironmentVariable('ARS_NDJ_BIND_PROBE','1','Machine')`
2. `Stop-Service ARAdminSvc -Force` (wait for arssvc.exe to exit).
3. Copy the rebuilt `ActiveRoles.Service.dll` (from the Service project bin, x64\Debug — the same
   source used before: `D:\src\ActiveRoles\Service\out\x64\Debug\ActiveRoles.Service.dll` after
   the rebuild, or the project bin) over
   `C:\Program Files\One Identity\Active Roles\8.5\Service\ActiveRoles.Service.dll`. (OI.Ldap /
   Kerberos.NET already deployed.) Confirm the install dll is the freshly built one (timestamp).
4. Delete any stale `C:\tmp\ndj_probe.log`.
5. `Start-Service ARAdminSvc`. Wait ~30–60s for full startup (listeners up on 15172).

## Observe + classify
Read `C:\tmp\ndj_probe.log` and classify:
- **#1 BIND SUCCESS** — "PROBE BIND OK" with a client type. THE WIN: real config-DB override
  creds -> DC discovery -> Kerberos bind worked live against fodor.monster.local\rsvc-ar.
- **#2 CLEAR DIAGNOSTIC** — our InvalidOperationException (no override / no domain / no params).
  (Not expected — override account is configured.)
- **#3 BIND FAILURE** — real bind/discovery exception. Capture the FULL exception+stack from the
  log. Likely candidates to note: `PlatformNotSupportedException` (multi-address TcpClient connect
  — known OI.Ldap Linux issue, but this is Windows so unlikely), Kerberos/SPN/realm, SSL, DNS-SRV
  "no DC found", or `Server.ProcessRequest` failing to load creds.
- If `C:\tmp\ndj_probe.log` is ABSENT: the probe branch didn't run — re-check ARS_NDJ_BIND_PROBE
  (Machine) was set before the service process started, and that the probe code is on the executed
  startup path. Also confirm the service actually started (Get-Service + port 15172).

Also grab: service state, arssvc PID, port 15172 listening y/n, and the built dll timestamp.

## Cleanup
- **REVERT the StartStop.cs edit** (`git checkout -- <StartStop.cs>`) so the feature branch/PR stays
  clean — the probe is throwaway and must NOT land in the PR. The deployed probe dll can remain for
  now (we'll redeploy a clean build later if needed).
- Do NOT git fetch. Do NOT push. Do NOT commit the probe.

## Report -> `D:\tmp\725472_probe_report.md` (UTF-8)
The classification (#1/#2/#3) with the exact `ndj_probe.log` contents, the bound client type (or
full exception+stack), service state + port, built dll timestamp, confirmation you reverted
StartStop.cs, and anything needing a master-HAL decision.
