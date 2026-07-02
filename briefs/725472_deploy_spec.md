# SPEC — Live-deploy the 9.0 private build to ar1 and prove DefaultLdapClientFactory binds (#725472)

mini-HAL on ar1. This is a LAB. No backup required (user directive: "do what it takes,
it's a lab"). Goal: run our freshly-built 9.0 `ClientConnections.DefaultLdapClientFactory`
inside the real ARS Administration Service so it loads the managed domain's override account
from the config DB, discovers a DC, and Kerberos-binds — then read the log to confirm.

Repo build output is already produced at `D:\src\ActiveRoles\Service\out\x64\Debug`
(arssvc.exe + all `ActiveRoles.*` 9.0 assemblies + `OneIdentity.Ldap.dll` + `Kerberos.NET.dll`,
built today). The installed service is stock 8.5 at
`C:\Program Files\One Identity\Active Roles\8.5\Service` (service name **ARAdminSvc**,
runs as **FODOR\rsvc-ar**). We overlay 9.0 over it. Expect possible 9.0-vs-8.5 DB-schema
friction; if the service won't start, that's a VALID result — capture the exact error.

## Steps

### 1. Stop the service
```
Stop-Service ARAdminSvc -Force
# confirm stopped; if arssvc.exe lingers, wait for it to exit
```

### 2. Overlay the 9.0 build over the install
Copy every file from `D:\src\ActiveRoles\Service\out\x64\Debug` into
`C:\Program Files\One Identity\Active Roles\8.5\Service`, **EXCLUDING test/build-harness
assemblies** (they must NOT land in the service dir). Exclude any file whose name matches:
`*Tests*`, `*.Fakes.dll`, `*Fakes.dll`, `ActiveRoles.Common.Testing.dll`,
`Microsoft.QualityTools.Testing.*`, `Microsoft.Testing.*`, `*.Fakes.FakedTypesCache`,
`mscorlib.*.Fakes.dll`, `System.*.Fakes.dll`, `Service.*.Tests.dll`, `vstest*`, `testhost*`,
`xunit*`, `nunit*`, `Moq*` (Moq only if present — do NOT ship test mocks into the service).
Copy `.dll`, `.exe`, `.config`, `.pdb`. **Must include:** `arssvc.exe`, `arssvc.exe.config`,
`ActiveRoles.Service.dll`, `ActiveRoles.Common.Services.ActiveDirectory.dll`,
`OneIdentity.Ldap.dll`, `Kerberos.NET.dll`, `ActiveRoles.Configuration.Common.dll`, and every
other non-test `ActiveRoles.*` 9.0 assembly. Overwrite existing 8.5 files. Do NOT delete the
install's extra files that aren't in the build (e.g. AspNetCore.*, native, MMC) — leave them.
Report the count copied and confirm `ActiveRoles.Service.dll` in the install is now 9.0.0.0
(`[Reflection.AssemblyName]::GetAssemblyName(...)` .Version).

### 3. Force NDJ mode
Two triggers (`NdjMode.IsEnabled()` = env var OR endpoints file). Set BOTH:
- Machine env var so the service (rsvc-ar) sees it:
  `[Environment]::SetEnvironmentVariable('ARS_NDJ_MODE','1','Machine')`
- Drop the endpoints file at the default path
  `%ProgramData%\OneIdentity\ActiveRoles\ndj-endpoints.json` — i.e.
  `C:\ProgramData\OneIdentity\ActiveRoles\ndj-endpoints.json` (create the dir). Minimal content
  pointing at the lab ARS host (adjust server name to the configured managed domain / this host):
  ```json
  { "servers": [ { "serverName": "ar1.fodor.monster.local", "port": 15172, "domainDN": "DC=fodor,DC=monster,DC=local" } ] }
  ```
  (This mainly satisfies the file-presence trigger + endpoint locator; the factory itself finds
  the DC via DNS SRV and loads creds from the config DB, so exact endpoint values are not critical
  to the bind test.)
- If the factory needs an explicit realm (hardened dir), you may also set
  `[Environment]::SetEnvironmentVariable('ARS_NDJ_KERBEROS_REALM','FODOR.MONSTER.LOCAL','Machine')`
  — optional; try WITHOUT first so RootDSE auto-discovery is exercised.

Confirm the configured managed domain exists: it was added today via Config Center with an
override/aux account. If you can cheaply query the config DB for the managed-domain DNS name +
`edsaUseOverrideAccount=TRUE`, note it — this is exactly what
`ClientConnections.LoadManagedDomainOverrideParameters` will read.

### 4. Ensure runtime tracing is on, then start
ARS service trace log lives under `C:\ProgramData\One Identity\Active Roles\Logs\` (Config Center
logs are there; the service runtime/EDS trace may be a separate file/registry-gated). Before start:
- Enable verbose service tracing if not already (registry under
  `HKLM\SOFTWARE\One Identity\Active Roles\...` Debug/Trace, or the standard EDS trace switch —
  grep how the codebase configures `Trace`/`ArsTrace.IsEnabled`). If unsure, at minimum rely on the
  Windows **Application** event log (ARS logs service/NDJ events there; our NDJ-active signal is an
  event from PBI #711229).
Then:
```
Start-Service ARAdminSvc   # may throw if 9.0 rejects the 8.5 DB — capture the exception
```

### 5. Observe the outcome
Within ~60–120s of start (or immediately on failure), collect:
- **Service state** (Running / Stopped) and, if it failed to start, the Service Control Manager
  error + the newest lines of any arssvc trace log and the Application event log entries for
  source(s) like `Active Roles`, `EDM`, `ARS`, `arssvc`.
- Grep the newest trace log(s) AND the Application event log for these markers:
  `NDJ`, `IdentityMode`, `DefaultLdapClientFactory`, `NdjLdapBinder`, `ClientConnections`,
  `LoadManagedDomainOverrideParameters`, `override account`, `Kerberos`, `bind`, `ConnectAndBind`,
  `DirSyncPool`, `RequestPool`, `#711232`, `no managed domain`, `PlatformNotSupported`.
- Classify the DefaultLdapClientFactory result into exactly one of:
  1. **BIND SUCCESS** — pools constructed, no exception (the win: real config-DB creds → DC discovery → Kerberos bind worked).
  2. **CLEAR DIAGNOSTIC** — our `InvalidOperationException` (no override account / no managed domain / no stored params) — means the path ran but config wasn't as expected.
  3. **BIND FAILURE** — a real bind/discovery exception (Kerberos, SSL, socket, PlatformNotSupported) — capture the full message + stack.
  4. **SERVICE DID NOT START** — 9.0/DB incompatibility before Initialize — capture the error.

## Report -> `D:\tmp\725472_deploy_report.md` (write as UTF-8)
- Files copied (count) + confirmation install `ActiveRoles.Service.dll` is now 9.0.0.0.
- The managed-domain DNS name + whether override account is set (if you could read it).
- Whether the service started on the 9.0 overlay (Y/N) + any SCM/DB error.
- The DefaultLdapClientFactory classification (1–4 above) with the exact log/event excerpts that
  prove it (trim to the proving lines).
- Anything that needs a master-HAL decision (e.g. "9.0 needs a DB upgrade", "override account not
  actually set on the domain", "realm auto-discovery failed, needs ARS_NDJ_KERBEROS_REALM").
- Do NOT git fetch. Do NOT push. Leave the overlay in place (we may iterate).
