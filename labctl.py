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
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_AUTOFEDORA = Path(os.environ.get("AUTOFEDORA_DIR", os.path.expanduser("~/work/Toolz/autofedora")))
LABNET_DEFAULT = "LABnet"  # real libvirt network name on HAL (case-sensitive)
QUEUE_PORT = 5001

# role -> autofedora -Role token (labctl accepts friendly aliases)
ROLE_ALIASES = {
    "dc": "dc", "ar": "ar", "ars": "ars", "sql": "sql",
    "exchange": "exchange", "exch": "exch", "member": "member", "srv": "srv",
}
# single-box roles that accept an explicit --name/--ip (base/full are multi-box)
SINGLE_BOX_ROLES = set(ROLE_ALIASES)
# default hive capability tags advertised at enrollment (override with --caps).
# The ws2025 baseline bakes in Node/Git/Copilot CLI — hence has:git/has:node.
DEFAULT_CAPS = "os:windows has:git has:node"


def log(msg: str) -> None:
    print(f"[labctl] {msg}")


def die(msg: str, code: int = 1):
    print(f"[labctl] ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


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


def run(cmd: list[str], *, dry: bool, check: bool = True, capture: bool = False,
        cwd: str | None = None) -> subprocess.CompletedProcess | None:
    """Run a command. In --dry-run, print and skip (returns None)."""
    printable = " ".join(cmd)
    if dry:
        log(f"DRY  {printable}")
        return None
    log(f"exec {printable}")
    return subprocess.run(cmd, check=check, cwd=cwd,
                          capture_output=capture, text=True)


def virsh(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["virsh", *args], capture_output=capture, text=True)


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


def preflight(name: str, role: str, ip: str, autofedora: Path, cfg: dict,
              labnet: str, *, force: bool) -> list[str]:
    """Return a list of problems (empty == good to go)."""
    problems: list[str] = []

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

    return problems


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_preflight(a) -> int:
    autofedora = Path(a.autofedora)
    cfg = read_labconfig(autofedora)
    problems = preflight(a.name, a.role, a.ip, autofedora, cfg, a.labnet, force=a.force)
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

    problems = preflight(a.name, a.role, a.ip, autofedora, cfg, a.labnet, force=a.force)
    if problems:
        log(f"PREFLIGHT FAIL — refusing to build '{a.name}':")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    log(f"PREFLIGHT OK — building '{a.name}' ({a.role}) ip={a.ip}")

    if a.force and virsh("dominfo", a.name).returncode == 0:
        log(f"--force: destroying existing '{a.name}' first")
        _destroy(a.name, autofedora, cfg, a.labnet, dry=a.dry_run)

    # 2. drive autofedora headlessly (long-running: dcpromo, role config, reboots)
    ps = [shutil.which("pwsh"), "-NoProfile", "-File", str(autofedora / "autofedora.ps1"),
          "-Role", a.role, "-Name", a.name, "-Ip", a.ip]
    if a.domain:
        ps += ["-DomainSuffix", a.domain]
    r = run(ps, dry=a.dry_run, check=False, cwd=str(autofedora))
    if r is not None and r.returncode != 0:
        die(f"autofedora provisioning failed (exit {r.returncode}) for '{a.name}'", r.returncode)

    # 3. enroll as a hive worker (unless opted out)
    if a.no_enroll:
        log("skipping hive enrollment (--no-enroll)")
    else:
        _enroll(a.name, a.role, a.ip, autofedora, lane=a.lane, caps=a.caps,
                hive_url=a.hive_url, dry=a.dry_run)

    log(f"DONE — '{a.name}' provisioned" + ("" if a.no_enroll else " and enrolled as a hive worker"))
    return 0


def _enroll(name: str, role: str, ip: str, autofedora: Path, *,
            lane: str | None, caps: str | None, hive_url: str | None, dry: bool) -> None:
    base = (hive_url or f"http://{local_ip()}:{QUEUE_PORT}").rstrip("/")
    lane = lane or name
    caps = caps or DEFAULT_CAPS
    q = urllib.parse.urlencode({"lane": lane, "caps": caps})
    guest_cmd = f"irm '{base}/bootstrap.ps1?{q}' | iex"
    helper = HERE / "labnet_winrm.ps1"
    log(f"enrolling '{name}' -> lane='{lane}' caps='{caps}' via {base}")
    run([shutil.which("pwsh"), "-NoProfile", "-File", str(helper),
         "-Ip", ip, "-AutofedoraDir", str(autofedora), "-Command", guest_cmd],
        dry=dry, check=True)


def cmd_destroy(a) -> int:
    autofedora = Path(a.autofedora)
    cfg = read_labconfig(autofedora)
    if virsh("dominfo", a.name).returncode != 0:
        log(f"no domain named '{a.name}' — nothing to destroy")
        # still attempt reservation/overlay cleanup below in case of a partial build
    if not a.yes and not a.dry_run:
        die(f"refusing to destroy '{a.name}' without --yes (or --dry-run to preview)")
    _destroy(a.name, autofedora, cfg, a.labnet, dry=a.dry_run)
    log(f"destroyed '{a.name}'")
    return 0


def _destroy(name: str, autofedora: Path, cfg: dict, labnet: str, *, dry: bool) -> None:
    # 1. stop + undefine (with nvram, since domains use OVMF)
    run(["virsh", "destroy", name], dry=dry, check=False)
    run(["virsh", "undefine", name, "--nvram"], dry=dry, check=False)

    # 2. remove the DHCP reservation (need name+ip+mac exactly as it was added)
    entry = vm_entry(cfg, name)
    ip = entry.get("IP") if entry else None
    if ip:
        mac = mac_for_ip(ip)
        host_xml = f"<host mac='{mac}' name='{name}' ip='{ip}'/>"
        run(["virsh", "net-update", labnet, "delete", "ip-dhcp-host", host_xml,
             "--live", "--config"], dry=dry, check=False)
    else:
        log(f"no lab-config IP for '{name}' — skipping DHCP-reservation cleanup "
            f"(remove manually if it was an ad-hoc box)")

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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="labctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--autofedora", default=str(DEFAULT_AUTOFEDORA),
                    help=f"autofedora repo dir (default: {DEFAULT_AUTOFEDORA})")
    ap.add_argument("--labnet", default=LABNET_DEFAULT, help="libvirt network name")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    sub = ap.add_subparsers(dest="command", required=True)

    def add_target(p, need_ip=True):
        p.add_argument("name", help="VM / hostname")
        p.add_argument("--role", required=True, choices=sorted(SINGLE_BOX_ROLES),
                       help="lab role (maps to autofedora -Role)")
        p.add_argument("--ip", default="", help="static IP (required for single-box roles)")
        p.add_argument("--force", action="store_true",
                       help="destroy+rebuild if the box/reservation already exists")

    p_pre = sub.add_parser("preflight", help="check prerequisites only")
    add_target(p_pre)

    p_new = sub.add_parser("new", help="preflight -> build -> enroll one box")
    add_target(p_new)
    p_new.add_argument("--domain", default="", help="AD domain suffix override")
    p_new.add_argument("--lane", default="", help="hive lane (default: the box name)")
    p_new.add_argument("--caps", default="", help=f"hive caps (default: '{DEFAULT_CAPS}')")
    p_new.add_argument("--hive-url", default="", help="queue base URL (default: http://<lab-ip>:5001)")
    p_new.add_argument("--no-enroll", action="store_true", help="do not register as a hive worker")
    p_new.add_argument("--host", default="", help="[reserved] remote KVM/Hyper-V target (not yet implemented)")

    p_del = sub.add_parser("destroy", help="undefine + overlay + DHCP-reservation cleanup")
    p_del.add_argument("name")
    p_del.add_argument("--yes", action="store_true", help="confirm destruction")

    sub.add_parser("list", help="list lab domains and hive workers")

    a = ap.parse_args(argv)
    # normalise empty strings the subparsers may not set
    for attr, default in (("lane", None), ("caps", None), ("hive_url", None),
                          ("domain", None), ("host", None), ("ip", ""),
                          ("force", False), ("no_enroll", False)):
        if not hasattr(a, attr):
            setattr(a, attr, default)
        elif getattr(a, attr) == "":
            setattr(a, attr, default if default is not None else "")

    if a.command == "preflight":
        return cmd_preflight(a)
    if a.command == "new":
        return cmd_new(a)
    if a.command == "destroy":
        return cmd_destroy(a)
    if a.command == "list":
        return cmd_list(a)
    ap.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
