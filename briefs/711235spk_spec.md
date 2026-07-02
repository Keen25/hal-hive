# Spike: PBI 711235 - NDJ runtime composition (single-source-of-truth mode + pool seam + DirSync selection)

You are mini-HAL on ar1. Implement this spike EXACTLY as specified. It is a COMPILING SCAFFOLD
spike (pooling is stubbed), BUT the mode-derivation change is real and production-intended.
Keep the domain-joined (Windows) path behavior identical to today.

## 0. Repo, branch (stacked on 725181 - do NOT branch off develop)
- Repo: D:\src\ActiveRoles (full clone). Currently on feature/725181_oi-ldap-dirsync-change-tracker
  @ 55cd34a101 (clean). Create the spike branch off CURRENT HEAD:
    git checkout -b feature/711235_ndj-runtime-composition-spike
  This spike consumes 725181's CreateForOiLdapTransport + OiLdapDirSyncChangeTracker (NOT on develop
  yet - PR #94311 unmerged). Do NOT fetch/reset develop. Do NOT modify the 725181 files.

## 1. Design decision (single source of truth - kills the registry split-brain)
Today the same "NDJ" fact is gated two incompatible ways:
- NdjMode.IsEnabled() (env ARS_NDJ_MODE=1 OR %ProgramData%\OneIdentity\ActiveRoles\ndj-endpoints.json)
  -> used by Shell.
- Registry DWord IdentityProvider (Windows=0/Ldap=1) -> read by ClientConnections + DirectoryDiscovery.
These can disagree. FIX: make NdjMode.IsEnabled() the single source of truth. ClientConnections and
DirectoryDiscovery derive their mode from NdjMode instead of the registry. The registry IdentityProvider
read is DELETED. Verified safe: only those 2 managed readers exist, NO native C++ reader, and with no
env var / no file NdjMode returns false => Windows (identical to today's default).

## 2. Grounded facts (verified - do not re-litigate)
- ClientConnections: Service/Source/Service/ClientConnections/ClientConnections.cs (namespace
  ActiveRoles.Service). Initialize(IConfigurationProvider configProvider = null); internal static
  IdentityProviderMode IdentityMode {get; private set;}; CreateCallerIdentity switch; Shutdown().
  ReadIdentityProviderMode() does the registry read at line ~215-225.
  Callers of Initialize (ALL no-arg): Service.Control/StartStop.cs:304, and test rigs
  (Service.Tests/TestEnvironment.cs, Service.DatabaseTests/TestEnvironment.cs).
- DirectoryDiscovery: Service/Source/Service/Domains/DirectoryDiscovery.cs (namespace ActiveRoles.Service).
  CreateCurrent() reads registry IdentityProvider via RegistryConfigurationProvider (ReadIdentityProviderMode
  helper, ~line 40-55) and picks DnsSrvDirectoryDiscovery (Ldap) vs Win32DirectoryDiscovery (Windows).
  Carries a TODO(723399) to replace the registry read.
- NdjMode: Common/Source/Common.Services.ActiveDirectory/Configuration/NdjMode.cs, namespace
  ActiveRoles.Common.Services.ActiveDirectory.Configuration. Static IsEnabled().
- Enum IdentityProviderMode { Windows, Ldap }: Common/Source/Configuration.Common/IdentityModes.cs
  (namespace ActiveRoles.Configuration.Common). KEEP the enum (used internally); just change its derivation.
- Transport seam (Service.ActiveDirectory, ns ActiveRoles.Service.ActiveDirectory):
  OiLdapDirSyncChangeTracker(OneIdentity.Ldap.ILdapClient);
  static DirectorySynchronizer.CreateForOiLdapTransport(string rootDN, OneIdentity.Ldap.ILdapClient,
  DirSyncRequestProcessor); legacy ctor new DirectorySynchronizer(string rootDN, DirSyncRequestProcessor).
  DirectorySynchronizer is constructed today ONLY at ADAM/AdamDirectorySynchronization.cs:31.
- Layering (verified): Service is upstream of Service.ActiveDirectory (Service does NOT reference it).
  Service references OneIdentity.Ldap (PackageReference). Service does NOT yet reference
  Common.Services.ActiveDirectory; adding that ProjectReference is ACYCLIC (that project does not
  reference Service). Service.ActiveDirectory is NOT in Service's InternalsVisibleTo -> new
  ClientConnections members consumed cross-assembly MUST be public.
- Test project: Service/Tests/Service.Tests (ActiveRoles.Service.Tests, IS in Service InternalsVisibleTo).

## 3. Deliverables

### 3a. Add ProjectReference
Add to Service/Source/Service/Service.csproj:
    <ProjectReference Include="..\..\..\Common\Source\Common.Services.ActiveDirectory\Common.Services.ActiveDirectory.csproj" />
(verify exact relative path/filename against the repo). If this creates a cycle or fails to load, STOP and report.

### 3b. Pool seam (Service project, new folder Service/Source/Service/Ldap/)
ILdapConnectionPool.cs (namespace ActiveRoles.Service.Ldap):
    public interface ILdapConnectionLease : System.IDisposable { OneIdentity.Ldap.ILdapClient Client { get; } }
    public interface ILdapConnectionPool : System.IDisposable { ILdapConnectionLease Acquire(); }
StubLdapConnectionPool.cs (namespace ActiveRoles.Service.Ldap):
    // SPIKE STUB (711235 follow-up): no real pooling/sizing/reuse. Builds a client per Acquire() via the
    // injected factory; disposes it on lease Dispose. Real leasing + min/max sizing is follow-up.
    public sealed class StubLdapConnectionPool : ILdapConnectionPool {
        private readonly System.Func<OneIdentity.Ldap.ILdapClient> _clientFactory;
        public StubLdapConnectionPool(System.Func<OneIdentity.Ldap.ILdapClient> clientFactory) {
            _clientFactory = clientFactory ?? throw new System.ArgumentNullException(nameof(clientFactory));
        }
        public ILdapConnectionLease Acquire() => new Lease(_clientFactory());
        public void Dispose() { }
        private sealed class Lease : ILdapConnectionLease {
            public Lease(OneIdentity.Ldap.ILdapClient client) { Client = client; }
            public OneIdentity.Ldap.ILdapClient Client { get; }
            public void Dispose() { (Client as System.IDisposable)?.Dispose(); }
        }
    }

### 3c. ClientConnections: derive mode from NdjMode + own pools + expose IsNonDomainJoined
- Replace the registry derivation. In Initialize(), set:
    IdentityMode = ActiveRoles.Common.Services.ActiveDirectory.Configuration.NdjMode.IsEnabled()
        ? IdentityProviderMode.Ldap : IdentityProviderMode.Windows;
  Delete the ReadIdentityProviderMode() helper and its registry use. The configProvider parameter is
  now unused for the mode read; change the signature to:
    public static void Initialize(System.Func<OneIdentity.Ldap.ILdapClient> ldapClientFactory = null)
  (all callers are no-arg, so dropping configProvider is safe; if any caller still passes it, STOP+report.)
- Add PUBLIC accessors (consumed cross-assembly):
    public static bool IsNonDomainJoined => IdentityMode == IdentityProviderMode.Ldap;
    public static ActiveRoles.Service.Ldap.ILdapConnectionPool DirSyncPool { get; private set; }
    public static ActiveRoles.Service.Ldap.ILdapConnectionPool RequestPool { get; private set; }
- In Initialize(), when IsNonDomainJoined, build the two pools (per 711235: separate request vs dirsync):
    if (IdentityMode == IdentityProviderMode.Ldap) {
        var factory = ldapClientFactory ?? DefaultLdapClientFactory;
        RequestPool = new ActiveRoles.Service.Ldap.StubLdapConnectionPool(factory);
        DirSyncPool = new ActiveRoles.Service.Ldap.StubLdapConnectionPool(factory);
    }
  private static OneIdentity.Ldap.ILdapClient DefaultLdapClientFactory() =>
      throw new System.NotImplementedException(
          "711235 follow-up: construct a bound OneIdentity.Ldap.ILdapClient from ndj-endpoints.json.");
- In Shutdown(): DirSyncPool?.Dispose(); RequestPool?.Dispose(); DirSyncPool = null; RequestPool = null;
- Windows mode: pools stay null; DJ path behavior identical to today.

### 3d. DirectoryDiscovery: derive from NdjMode (remove registry read + TODO 723399)
In Service/Source/Service/Domains/DirectoryDiscovery.cs, change CreateCurrent() to derive from NdjMode
instead of the registry:
    bool ndj = ActiveRoles.Common.Services.ActiveDirectory.Configuration.NdjMode.IsEnabled();
    if (ndj) { ... return new DnsSrvDirectoryDiscovery(...); }   // unchanged branch body
    return new Win32DirectoryDiscovery();
Delete the ReadIdentityProviderMode() helper and the RegistryConfigurationProvider read here, and
remove the TODO(723399) comment. Keep the existing DnsSrv/Win32 construction bodies unchanged.

### 3e. Transport decision + factory (Service.ActiveDirectory)
New DirectorySynchronization/DirSyncTransport.cs (ns ActiveRoles.Service.ActiveDirectory):
    public enum DirSyncTransport { Classic, OiLdap }
    public static class DirSyncTransportSelector {
        public static DirSyncTransport Select(bool isNonDomainJoined, bool dirSyncPoolAvailable) =>
            (isNonDomainJoined && dirSyncPoolAvailable) ? DirSyncTransport.OiLdap : DirSyncTransport.Classic;
    }
New DirectorySynchronization/DirectorySynchronizerFactory.cs (ns ActiveRoles.Service.ActiveDirectory):
    // Composition seam for on-prem AD DirSync. No production on-prem AD DirSync driver is wired today
    // (DirectorySynchronizer is only built for ADAM). This factory is the intended future call site;
    // for the spike it is exercised by unit tests only. Wiring the live sync loop is follow-up (725428).
    public static class DirectorySynchronizerFactory {
        public static DirectorySynchronizer CreateForActiveDirectory(
                string rootDN, DirSyncRequestProcessor requestProcessor) {
            bool poolAvailable = ActiveRoles.Service.ClientConnections.DirSyncPool != null;
            var transport = DirSyncTransportSelector.Select(
                ActiveRoles.Service.ClientConnections.IsNonDomainJoined, poolAvailable);
            if (transport == DirSyncTransport.OiLdap) {
                var lease = ActiveRoles.Service.ClientConnections.DirSyncPool.Acquire();
                // Spike: lease lifetime follows the synchronizer; real return/reuse is 711235 follow-up.
                return DirectorySynchronizer.CreateForOiLdapTransport(rootDN, lease.Client, requestProcessor);
            }
            return new DirectorySynchronizer(rootDN, requestProcessor);
        }
    }

## 4. Tests (Service/Tests/Service.Tests/DirectorySynchronization/NdjRuntimeCompositionTests.cs)
Match the MSTest style + framework already used by OiLdapDirSyncChangeTrackerTests.cs in that folder.
Do NOT add new NuGet packages; if a mock is needed and Moq is not already referenced, hand-write a
minimal fake ILdapClient. Cover:
1. Select(false,false)==Classic; Select(false,true)==Classic; Select(true,false)==Classic; Select(true,true)==OiLdap.
2. StubPool.Acquire().Client returns the factory's client instance.
3. StubPool lease Dispose disposes an IDisposable client.

## 5. Build + test
- Full solution build (C++/CLI codegen ordering - do NOT build the leaf csproj alone):
    D:\VS2022\MSBuild\Current\Bin\amd64\MSBuild.exe Service\ActiveRoles.Service.sln /p:Configuration=Debug /p:Platform=x64 /m
- Tests: vstest.console.exe (under D:\VS2022\Common7\IDE\CommonExtensions\Microsoft\TestWindow\)
    Service\out\AnyCPU\Debug\ActiveRoles.Service.Tests.dll /TestCaseFilter:"FullyQualifiedName~NdjRuntimeComposition"
- Iterate until 0 build errors and all new tests green. Also confirm the solution still builds as a whole.

## 6. Commit (do NOT push)
git add new/changed files; commit message:
    711235 - Spike: NDJ single-source-of-truth mode (NdjMode-derived) + DirSync transport selection + pool seam
Trailer: Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Do NOT push - master-HAL reviews the diff first.

## 7. Report -> D:\tmp\711235spk_report.md
Include: branch + commit sha; files added/changed; build errors/warnings count; test pass/fail count;
anything not implemented or deviated; any blocker (esp. layering/reference). If blocked, STOP and
report rather than inventing scope.
