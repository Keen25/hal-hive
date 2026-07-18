#!/usr/bin/env python3
"""labctl — strict, first-try-reliable provisioning of a single lab VM/role.

labctl is a thin *non-interactive orchestrator* around the proven autofedora
engine (~/work/Toolz/autofedora/autofedora.ps1). It does the three things a bare
`autofedora.ps1 -Role ...` run does not:

  1. PREFLIGHT the documented prerequisites so a run fails in <1s with a clear
     message instead of half-building a box (the reason manual builds never
     completed first-try): name/IP free, baseline present, LABNet up, cred
     available, WinRM toolchain present.
  2. DRIVE autofedora headlessly for exactly one role/box.
  3. ENROLL the finished box as a hive worker (runs the queue server's
     bootstrap.ps1 one-liner in-guest over WinRM via labnet_winrm.ps1).

Scope (v1): the LOCAL KVM host this runs on (HAL). autofedora calls local
`virsh`/`qemu-img`, so remote KVM (Lenin) and Hyper-V are a documented follow-up
(--host is reserved and refused for now rather than pretending to work).

    labctl preflight <name> --role <r> --ip <ip>   # checks only, no changes
    labctl new       <name> --role <r> --ip <ip>   # preflight -> build -> enroll
    labctl destroy   <name> [--yes]                # undefine + overlay + DHCP reservation
    labctl list                                    # lab domains + hive workers

Add --dry-run to any command to print the plan without touching the system.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_AUTOFEDORA = Path(os.environ.get("AUTOFEDORA_DIR", os.path.expanduser("~/work/Toolz/autofedora")))
LABNET_DEFAULT = "LABNet"  # real libvirt network name on HAL (case-sensitive, qemu:///system)
# The real lab lives in the SYSTEM libvirt instance. A regular user's bare
# `virsh` defaults to qemu:///session (empty except stray session domains), so
# every labctl virsh/autofedora call must pin the system URI or it inspects the
# wrong scope — falsely reporting names free, and (worst) deleting a live VM's
# storage while its system-scoped domain survives. autofedora.ps1 forces the
# same URI; keep them in lockstep.
LIBVIRT_URI = os.environ.get("LIBVIRT_DEFAULT_URI") or "qemu:///system"
QUEUE_PORT = 5001
HIVE_ENV = Path(os.path.expanduser("~/.hive/hive.env"))
WORKER_TOKEN_FILE = Path(os.path.expanduser("~/.hive/worker.token"))

# role -> autofedora -Role token (labctl accepts friendly aliases)
ROLE_ALIASES = {
    "dc": "dc", "ar": "ar", "ars": "ars", "sql": "sql",
    "exchange": "exchange", "exch": "exch", "member": "member", "srv": "srv",
}
# single-box roles that accept an explicit --name/--ip (base/full are multi-box)
SINGLE_BOX_ROLES = set(ROLE_ALIASES)
# Roles whose autofedora path does NOT install the product itself (it only base-
# configures + domain-joins + sets up an IIS/cert host). Building these leaves a
# box WITHOUT the product, so labctl refuses them unless --allow-partial is given
# — otherwise "provisioned" would be a half-truth (the exact half-build trap).
PARTIAL_ROLES = {"ar", "ars"}
# default hive capability tags advertised at enrollment (override with --caps).
# The ws2025 baseline bakes in Node/Git/Copilot CLI — hence has:git/has:node.
DEFAULT_CAPS = "os:windows has:git has:node"


def log(msg: str) -> None:
    print(f"[labctl] {msg}")


def die(msg: str, code: int = 1):
    print(f"[labctl] ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _libvirt_env() -> dict:
    """os.environ with LIBVIRT_DEFAULT_URI pinned to the system instance."""
    env = dict(os.environ)
    env["LIBVIRT_DEFAULT_URI"] = LIBVIRT_URI
    return env


def _read_hive_env() -> dict:
    """Parse ~/.hive/hive.env (bare KEY=VALUE lines; inline # is NOT a comment)."""
    out: dict[str, str] = {}
    if not HIVE_ENV.is_file():
        return out
    for line in HIVE_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _control_token() -> str | None:
    """Resolve the hive control token host-side (never sent to the guest).

    Order mirrors queue_server._load_token: $HIVE_TOKEN, then hive.env HIVE_TOKEN,
    then ~/.hive/worker.token. Used to authenticate the /bootstrap.ps1 fetch,
    which queue_server gates behind the control token (it embeds live secrets)."""
    tok = os.environ.get("HIVE_TOKEN")
    if tok:
        return tok.strip()
    tok = _read_hive_env().get("HIVE_TOKEN")
    if tok:
        return tok.strip()
    if WORKER_TOKEN_FILE.is_file():
        return WORKER_TOKEN_FILE.read_text().strip()
    return None


def _queue_base(hive_url: str | None) -> str:
    """Queue base URL a fresh worker dials back on.

    Explicit --hive-url wins; else HIVE_QUEUE_URL from env/hive.env; else the
    lab-facing IP + port (5001 unless HIVE_QUEUE_URL overrides it)."""
    if hive_url:
        return hive_url.rstrip("/")
    env_url = os.environ.get("HIVE_QUEUE_URL") or _read_hive_env().get("HIVE_QUEUE_URL")
    if env_url:
        return env_url.rstrip("/")
    return f"http://{local_ip()}:{QUEUE_PORT}"


def local_ip() -> str:
    """Primary lab-facing IP (what a new worker dials back on)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.100.1", 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _queue_reachable(base: str) -> bool:
    """True if the queue server answers /health at `base` (proves the callback
    URL a worker will use is actually live — local_ip alone doesn't, since the
    loopback route to 192.168.100.1 resolves even with the lab bridge down)."""
    try:
        req = urllib.request.Request(base.rstrip("/") + "/health")
        with urllib.request.urlopen(req, timeout=4) as r:
            return 200 <= r.status < 500
    except (urllib.error.URLError, OSError):
        return False


def _fetch_bootstrap(base: str, lane: str, caps: str, token: str) -> str:
    """Fetch the worker bootstrap script host-side with the control token.

    Done on the host (not via an in-guest `irm`) because /bootstrap.ps1 requires
    the control token and embeds live secrets — the guest one-liner carried no
    auth and got a 401. The returned self-contained script is then run in-guest
    over WinRM, so the token never touches the guest command line."""
    q = urllib.parse.urlencode({"lane": lane, "caps": caps})
    url = f"{base.rstrip('/')}/bootstrap.ps1?{q}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")


def run(cmd: list[str], *, dry: bool, check: bool = True, capture: bool = False,
        cwd: str | None = None) -> subprocess.CompletedProcess | None:
    """Run a command. In --dry-run, print and skip (returns None)."""
    printable = " ".join(cmd)
    if dry:
        log(f"DRY  {printable}")
        return None
    log(f"exec {printable}")
    return subprocess.run(cmd, check=check, cwd=cwd, env=_libvirt_env(),
                          capture_output=capture, text=True)


def virsh(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["virsh", *args], capture_output=capture, text=True,
                          env=_libvirt_env())


def mac_for_ip(ip: str) -> str:
    """Reproduce autofedora's Get-MacForIp: 52:54:00:64:00:<last octet hex>."""
    last = int(ip.strip().split(".")[-1])
    return "52:54:00:64:00:%02x" % last


# ── lab-config.psd1 is the single source of truth; read it via pwsh ─────────

def read_labconfig(autofedora: Path) -> dict:
    cfg_file = autofedora / "lab-config.psd1"
    if not cfg_file.is_file():
        die(f"lab-config.psd1 not found in {autofedora} (set --autofedora or $AUTOFEDORA_DIR)")
    pwsh = shutil.which("pwsh")
    if not pwsh:
        die("pwsh (PowerShell 7+) not found on PATH")
    r = subprocess.run(
        [pwsh, "-NoProfile", "-Command",
         f"Import-PowerShellDataFile -LiteralPath '{cfg_file}' | ConvertTo-Json -Depth 8"],
        capture_output=True, text=True)
    if r.returncode != 0:
        die(f"could not parse lab-config.psd1: {r.stderr.strip()}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        die(f"lab-config.psd1 did not yield valid JSON: {e}")


def vm_entry(cfg: dict, name: str) -> dict | None:
    """Find a lab-config VM definition whose Name matches (case-insensitive)."""
    for v in (cfg.get("VMs") or {}).values():
        if isinstance(v, dict) and str(v.get("Name", "")).lower() == name.lower():
            return v
    return None


# ── preflight ───────────────────────────────────────────────────────────────

def _cred_available(autofedora: Path, cfg: dict) -> bool:
    if cfg.get("AdminPassword"):
        return True
    cred_dir = Path(os.environ.get("AUTOFEDORA_CRED_DIR", os.path.expanduser("~/.config/autofedora")))
    return (cred_dir / "admin.cred").is_file() and (cred_dir / "admin.key").is_file()


def _ip_in_use(ip: str, labnet: str) -> bool:
    """True if the IP answers to ping or already holds a DHCP lease."""
    if subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                      capture_output=True).returncode == 0:
        return True
    r = virsh("net-dhcp-leases", labnet)
    return r.returncode == 0 and ip in r.stdout


def _reservation_conflict(name: str, ip: str, labnet: str) -> str | None:
    """Return a message if LABNet already reserves this name or IP for another host."""
    r = virsh("net-dumpxml", labnet)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("<host "):
            continue
        has_ip = f"ip='{ip}'" in line
        has_name = f"name='{name}'" in line
        if has_ip or has_name:
            return f"LABNet already has a reservation matching name/ip: {line}"
    return None


def _media_problems(role: str, cfg: dict, autofedora: Path) -> list[str]:
    """Role-specific installer-media checks (fail fast, before any VM is built).

    Exchange is the only single-box role whose autofedora path consumes host
    media: the Exchange ISO plus the UCMA / VC++ / URL-rewrite prereqs, all
    resolved relative to the autofedora dir ($PSScriptRoot). autofedora only
    *warned* about these at mount time and then half-built the box."""
    problems: list[str] = []
    if role in ("exchange", "exch"):
        iso = (cfg.get("EXCHANGE_ISO") or "").strip()
        if not iso:
            problems.append("EXCHANGE_ISO not set in lab-config.psd1")
        elif not (autofedora / iso).is_file() and not Path(iso).is_file():
            problems.append(f"Exchange ISO missing: {iso} (looked in {autofedora} and as an absolute path)")
        for fn in ("UcmaRuntimeSetup.exe", "vcredist_x64.exe", "rewrite_amd64_en-US.msi"):
            if not (autofedora / fn).is_file():
                problems.append(f"Exchange prereq missing: {autofedora / fn}")
    return problems


def preflight(name: str, role: str, ip: str, autofedora: Path, cfg: dict,
              labnet: str, *, force: bool, allow_partial: bool = False) -> list[str]:
    """Return a list of problems (empty == good to go)."""
    problems: list[str] = []

    # Roles whose engine path does not install the product would "succeed" while
    # leaving a box without it — refuse unless the caller acknowledges that.
    if role in PARTIAL_ROLES and not allow_partial:
        problems.append(
            f"role '{role}' base-configures + domain-joins + sets up IIS/cert but does NOT "
            f"install Active Roles itself (product install is a separate step). "
            f"Re-run with --allow-partial to build the base box anyway.")

    if not (autofedora / "autofedora.ps1").is_file():
        problems.append(f"autofedora.ps1 not found in {autofedora}")
    if not shutil.which("pwsh"):
        problems.append("pwsh (PowerShell 7+) not on PATH")
    if not shutil.which("virsh"):
        problems.append("virsh (libvirt) not on PATH")
        return problems  # nothing below works without virsh

    # libvirt reachable
    if virsh("uri").returncode != 0:
        problems.append("cannot reach libvirt (add yourself to the 'libvirt' group?)")

    # LABNet active
    r = virsh("net-info", labnet)
    if r.returncode != 0:
        problems.append(f"libvirt network '{labnet}' not found")
    else:
        active = next((ln.split(":", 1)[1].strip().lower()
                       for ln in r.stdout.splitlines() if ln.lower().startswith("active:")), "")
        if active != "yes":
            problems.append(f"libvirt network '{labnet}' is not active (virsh net-start {labnet})")

    # baseline disk present (autofedora clones $BaselineDisk for every role)
    baseline = cfg.get("BaselineDisk")
    if not baseline:
        problems.append("BaselineDisk not set in lab-config.psd1")
    elif not Path(baseline).is_file():
        problems.append(f"baseline disk missing: {baseline}")

    # credential resolvable (labctl runs autofedora non-interactively)
    if not _cred_available(autofedora, cfg):
        problems.append("no lab credential (set AdminPassword in lab-config.psd1 "
                        "or create the encrypted cred file via set-autofedora-cred.ps1)")

    # gss-ntlmssp needed for WinRM/Negotiate from Linux (enroll step). It is a
    # GSSAPI mech plugin, not an ldconfig lib — probe the registered mech / .so.
    if sys.platform.startswith("linux"):
        have_gss = (Path("/etc/gss/mech.d/ntlmssp.conf").is_file()
                    or any(Path(p).is_file() for p in
                           ("/usr/lib64/gssntlmssp/gssntlmssp.so",
                            "/usr/lib/x86_64-linux-gnu/gssntlmssp/gssntlmssp.so")))
        if not have_gss:
            problems.append("gss-ntlmssp not installed (dnf install gssntlmssp) — needed for WinRM enroll")

    # name free
    if virsh("dominfo", name).returncode == 0 and not force:
        problems.append(f"a domain named '{name}' already exists (use --force to destroy+rebuild)")

    # IP sanity + collision
    if role in SINGLE_BOX_ROLES:
        if not ip:
            problems.append(f"role '{role}' needs an explicit --ip")
        else:
            conflict = _reservation_conflict(name, ip, labnet)
            # a reservation for THIS name is fine when --force (we rebuild it)
            if conflict and not (force and f"name='{name}'" in conflict and f"ip='{ip}'" in conflict):
                problems.append(conflict)
            if _ip_in_use(ip, labnet) and not force:
                problems.append(f"{ip} is already in use (ping/lease) — pick a free static IP or --force")

    # role-specific installer media (exchange ISO + prereqs, etc.)
    problems += _media_problems(role, cfg, autofedora)

    return problems


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_preflight(a) -> int:
    autofedora = Path(a.autofedora)
    cfg = read_labconfig(autofedora)
    problems = preflight(a.name, a.role, a.ip, autofedora, cfg, a.labnet,
                         force=a.force, allow_partial=a.allow_partial)
    if problems:
        log(f"PREFLIGHT FAIL for '{a.name}' ({a.role}):")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    log(f"PREFLIGHT OK — '{a.name}' ({a.role}) ip={a.ip} baseline={cfg.get('BaselineDisk')}")
    return 0


def cmd_new(a) -> int:
    autofedora = Path(a.autofedora)
    if a.host:
        die("--host (remote KVM / Hyper-V) is not implemented yet; run labctl on the "
            "target KVM host. Remote provisioning is the next increment.")
    cfg = read_labconfig(autofedora)

    problems = preflight(a.name, a.role, a.ip, autofedora, cfg, a.labnet,
                         force=a.force, allow_partial=a.allow_partial)
    if problems:
        log(f"PREFLIGHT FAIL — refusing to build '{a.name}':")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    log(f"PREFLIGHT OK — building '{a.name}' ({a.role}) ip={a.ip}")

    if a.force and virsh("dominfo", a.name).returncode == 0:
        log(f"--force: destroying existing '{a.name}' first")
        _destroy(a.name, autofedora, cfg, a.labnet, ip=a.ip, dry=a.dry_run)

    # 2. drive autofedora headlessly (long-running: dcpromo, role config, reboots)
    ps = [shutil.which("pwsh"), "-NoProfile", "-File", str(autofedora / "autofedora.ps1"),
          "-Role", a.role, "-Name", a.name, "-Ip", a.ip]
    if a.domain:
        ps += ["-DomainSuffix", a.domain]
    r = run(ps, dry=a.dry_run, check=False, cwd=str(autofedora))
    if r is not None and r.returncode != 0:
        die(f"autofedora provisioning failed (exit {r.returncode}) for '{a.name}'", r.returncode)

    # 3. enroll as a hive worker (unless opted out). Enrollment failure does NOT
    #    destroy the (successfully built) box — report the partial state + retry.
    if a.no_enroll:
        log("skipping hive enrollment (--no-enroll)")
    else:
        try:
            _enroll(a.name, a.role, a.ip, autofedora, lane=a.lane, caps=a.caps,
                    hive_url=a.hive_url, dry=a.dry_run)
        except EnrollError as e:
            log(f"PROVISIONED but NOT ENROLLED: {e}")
            log(f"the VM '{a.name}' is up; retry enrollment with: "
                f"labctl enroll {a.name} --ip {a.ip} --role {a.role}"
                + (f" --lane {a.lane}" if a.lane else "")
                + (f" --hive-url {a.hive_url}" if a.hive_url else ""))
            return 2

    log(f"DONE — '{a.name}' provisioned" + ("" if a.no_enroll else " and enrolled as a hive worker"))
    return 0


class EnrollError(RuntimeError):
    """Enrollment failed AFTER the VM was successfully provisioned."""


def _enroll(name: str, role: str, ip: str, autofedora: Path, *,
            lane: str | None, caps: str | None, hive_url: str | None, dry: bool) -> None:
    base = _queue_base(hive_url)
    lane = lane or name
    caps = caps or DEFAULT_CAPS
    helper = HERE / "labnet_winrm.ps1"
    log(f"enrolling '{name}' -> lane='{lane}' caps='{caps}' via {base}")

    if dry:
        log(f"DRY  fetch {base}/bootstrap.ps1 (control-token auth) and run in-guest on {ip}")
        return

    # queue reachability first — local_ip() alone can advertise a dead callback.
    if not _queue_reachable(base):
        raise EnrollError(
            f"queue server not reachable at {base}/health — start it or pass "
            f"--hive-url with the address the guest can actually reach.")
    token = _control_token()
    if not token:
        raise EnrollError(
            "no hive control token found (set $HIVE_TOKEN, or HIVE_TOKEN in "
            "~/.hive/hive.env, or create ~/.hive/worker.token) — /bootstrap.ps1 "
            "requires it.")
    try:
        script = _fetch_bootstrap(base, lane, caps, token)
    except urllib.error.HTTPError as e:
        raise EnrollError(f"bootstrap fetch got HTTP {e.code} from {base} "
                          f"(control token rejected?)") from e
    except (urllib.error.URLError, OSError) as e:
        raise EnrollError(f"bootstrap fetch failed: {e}") from e

    # Hand the self-contained script to the guest via a temp file so the (secret-
    # bearing) body never appears on a command line. labnet_winrm runs it with
    # Invoke-Command -FilePath over WinRM.
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(script)
            tmp = fh.name
        r = subprocess.run(
            [shutil.which("pwsh"), "-NoProfile", "-File", str(helper),
             "-Ip", ip, "-AutofedoraDir", str(autofedora), "-ScriptFile", tmp],
            env=_libvirt_env(), text=True)
        if r.returncode != 0:
            raise EnrollError(f"in-guest bootstrap failed (labnet_winrm exit {r.returncode})")
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def cmd_enroll(a) -> int:
    """Idempotent standalone enrollment for an already-provisioned box."""
    autofedora = Path(a.autofedora)
    if not a.ip:
        die("enroll needs --ip (the box's reachable address)")
    try:
        _enroll(a.name, a.role, a.ip, autofedora, lane=a.lane, caps=a.caps,
                hive_url=a.hive_url, dry=a.dry_run)
    except EnrollError as e:
        die(str(e), 2)
    log(f"DONE — '{a.name}' enrolled as a hive worker")
    return 0


def cmd_destroy(a) -> int:
    autofedora = Path(a.autofedora)
    cfg = read_labconfig(autofedora)
    if virsh("dominfo", a.name).returncode != 0:
        log(f"no domain named '{a.name}' — nothing to destroy")
        # still attempt reservation/overlay cleanup below in case of a partial build
    if not a.yes and not a.dry_run:
        die(f"refusing to destroy '{a.name}' without --yes (or --dry-run to preview)")
    _destroy(a.name, autofedora, cfg, a.labnet, ip=a.ip, dry=a.dry_run)
    log(f"destroyed '{a.name}'")
    return 0


def _domain_reservation(name: str) -> tuple[str, str] | None:
    """Return (mac, ip) for a defined domain by reading its interface + lease.

    Lets destroy clean up ad-hoc boxes (not in lab-config) and boxes built with
    an overridden IP, instead of only lab-config names. MAC comes from the
    domain's interface; IP from the live DHCP lease for that MAC."""
    r = virsh("domiflist", name)
    if r.returncode != 0:
        return None
    mac = None
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and ":" in parts[-1]:
            mac = parts[-1]
            break
    if not mac:
        return None
    # derive IP from the reservation MAC scheme (52:54:00:64:00:XX -> .XX)
    try:
        octet = int(mac.split(":")[-1], 16)
        return mac, f"192.168.100.{octet}"
    except ValueError:
        return mac, ""


def _destroy(name: str, autofedora: Path, cfg: dict, labnet: str, *,
             ip: str | None = None, dry: bool) -> None:
    # Resolve MAC/IP BEFORE undefining (domiflist needs the domain to exist).
    dom_res = _domain_reservation(name)
    domain_existed = virsh("dominfo", name).returncode == 0

    # 1. stop + undefine (with nvram, since domains use OVMF). Capture results so
    #    we can fail CLOSED before deleting storage (deleting a live VM's disk
    #    while its domain survives would be catastrophic — the URI-scope bug).
    run(["virsh", "destroy", name], dry=dry, check=False)
    undef = run(["virsh", "undefine", name, "--nvram"], dry=dry, check=False)
    if not dry and domain_existed:
        still = virsh("dominfo", name).returncode == 0
        if still:
            rc = undef.returncode if undef is not None else "?"
            die(f"domain '{name}' still defined after undefine (exit {rc}) — "
                f"refusing to delete its storage/reservation. Resolve the domain "
                f"first (check 'virsh -c {LIBVIRT_URI} dominfo {name}').")

    # 2. remove the DHCP reservation. Prefer an explicit --ip, then the domain's
    #    own MAC/IP (works for ad-hoc + overridden-IP boxes), then lab-config.
    res_ip, res_mac = None, None
    if ip:
        res_ip, res_mac = ip, mac_for_ip(ip)
    elif dom_res:
        res_mac, res_ip = dom_res[0], dom_res[1] or None
    else:
        entry = vm_entry(cfg, name)
        cfg_ip = entry.get("IP") if entry else None
        if cfg_ip:
            res_ip, res_mac = cfg_ip, mac_for_ip(cfg_ip)
    if res_ip and res_mac:
        host_xml = f"<host mac='{res_mac}' name='{name}' ip='{res_ip}'/>"
        run(["virsh", "net-update", labnet, "delete", "ip-dhcp-host", host_xml,
             "--live", "--config"], dry=dry, check=False)
    else:
        log(f"could not determine DHCP reservation for '{name}' — pass --ip to "
            f"clean it up (remove manually if it was an ad-hoc box)")

    # 3. remove the qcow2 overlay + nvram var file
    store = cfg.get("VMStorePath")
    if store:
        overlay = Path(store) / f"{name}_OS.qcow2"
        if dry:
            log(f"DRY  rm {overlay}")
        elif overlay.exists():
            log(f"rm {overlay}")
            overlay.unlink()
    nvram = Path(cfg.get("NVRAMPath", "/var/lib/libvirt/qemu/nvram")) / f"{name}_VARS.fd"
    if dry:
        log(f"DRY  rm {nvram}")
    elif nvram.exists():
        log(f"rm {nvram}")
        nvram.unlink()


def cmd_list(a) -> int:
    print("== lab domains (virsh) ==")
    r = virsh("list", "--all")
    print(r.stdout.strip() if r.returncode == 0 else "  (virsh unavailable)")
    halctl = HERE / "halctl.py"
    if halctl.is_file():
        print("\n== hive workers (halctl) ==")
        w = subprocess.run([sys.executable, str(halctl), "workers"],
                           capture_output=True, text=True)
        print((w.stdout or w.stderr).strip())
    return 0


def _validate_hive_url(url: str) -> None:
    """Reject a --hive-url that isn't a plain http(s) URL (it's interpolated
    into requests + shown to the guest bootstrap; keep it clean)."""
    if not url:
        return
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        die(f"--hive-url must be an http(s) URL (got '{url}')")


def main(argv=None) -> int:
    # Global options live on a shared parent parser so they are accepted BOTH
    # before and after the subcommand (argparse otherwise only sees them before).
    gopts = argparse.ArgumentParser(add_help=False)
    gopts.add_argument("--autofedora", default=str(DEFAULT_AUTOFEDORA),
                       help=f"autofedora repo dir (default: {DEFAULT_AUTOFEDORA})")
    gopts.add_argument("--labnet", default=LABNET_DEFAULT, help="libvirt network name")
    gopts.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")

    ap = argparse.ArgumentParser(prog="labctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 parents=[gopts])
    sub = ap.add_subparsers(dest="command", required=True)

    def add_target(p):
        p.add_argument("name", help="VM / hostname")
        p.add_argument("--role", required=True, choices=sorted(SINGLE_BOX_ROLES),
                       help="lab role (maps to autofedora -Role)")
        p.add_argument("--ip", default="", help="static IP (required for single-box roles)")
        p.add_argument("--force", action="store_true",
                       help="destroy+rebuild if the box/reservation already exists")
        p.add_argument("--allow-partial", action="store_true",
                       help="build roles whose product install is not automated (ar/ars)")

    def add_enroll_opts(p):
        p.add_argument("--lane", default="", help="hive lane (default: the box name)")
        p.add_argument("--caps", default="", help=f"hive caps (default: '{DEFAULT_CAPS}')")
        p.add_argument("--hive-url", default="", help="queue base URL (default: http://<lab-ip>:5001)")

    p_pre = sub.add_parser("preflight", help="check prerequisites only", parents=[gopts])
    add_target(p_pre)

    p_new = sub.add_parser("new", help="preflight -> build -> enroll one box", parents=[gopts])
    add_target(p_new)
    add_enroll_opts(p_new)
    p_new.add_argument("--domain", default="", help="AD domain suffix override")
    p_new.add_argument("--no-enroll", action="store_true", help="do not register as a hive worker")
    p_new.add_argument("--host", default="", help="[reserved] remote KVM/Hyper-V target (not yet implemented)")

    p_enr = sub.add_parser("enroll", help="enroll an already-built box as a hive worker", parents=[gopts])
    p_enr.add_argument("name", help="VM / hostname")
    p_enr.add_argument("--role", default="", help="lab role (informational)")
    p_enr.add_argument("--ip", required=True, help="the box's reachable IP")
    add_enroll_opts(p_enr)

    p_del = sub.add_parser("destroy", help="undefine + overlay + DHCP-reservation cleanup", parents=[gopts])
    p_del.add_argument("name")
    p_del.add_argument("--ip", default="", help="reservation IP (for ad-hoc/overridden-IP boxes)")
    p_del.add_argument("--yes", action="store_true", help="confirm destruction")

    sub.add_parser("list", help="list lab domains and hive workers", parents=[gopts])

    a = ap.parse_args(argv)
    # normalise empty strings the subparsers may not set
    for attr, default in (("lane", None), ("caps", None), ("hive_url", None),
                          ("domain", None), ("host", None), ("ip", ""),
                          ("role", ""), ("force", False), ("no_enroll", False),
                          ("allow_partial", False)):
        if not hasattr(a, attr):
            setattr(a, attr, default)
        elif getattr(a, attr) == "":
            setattr(a, attr, default if default is not None else "")

    _validate_hive_url(a.hive_url or "")

    if a.command == "preflight":
        return cmd_preflight(a)
    if a.command == "new":
        return cmd_new(a)
    if a.command == "enroll":
        return cmd_enroll(a)
    if a.command == "destroy":
        return cmd_destroy(a)
    if a.command == "list":
        return cmd_list(a)
    ap.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
