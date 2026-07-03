#!/usr/bin/env python3
"""hostctl — stand up & manage a HAL hive CONTROL-PLANE host on any OS.

The queue logic is pure, OS-agnostic Python; what used to pin the hive to Linux
was (a) the git-http-backend path and (b) the systemd-only service assets. This
installer removes both: it detects git-http-backend via `git --exec-path` (see
git_http_server.py) and installs the control-plane processes as native services
on **Windows (WinSW)**, **Linux (systemd --user)** and **macOS (launchd)**.

One command turns a fresh box with Python 3.10+ and git into a hive host running:
  - queue_server.py    — REST control plane            (:5001)
  - git_http_server.py — smart-HTTP lab.git transport  (:8418)
  - worker_loop.py     — a local worker on lane 'hal'   (optional, on by default)

Bootstrap from nothing (one line; needs git + python on PATH):
  Linux/macOS:  git clone https://github.com/Keen25/hal-hive ~/hive && \
                python3 ~/hive/hostctl.py install
  Windows (PS): git clone https://github.com/Keen25/hal-hive $HOME\\hive; \
                python $HOME\\hive\\hostctl.py install

Subcommands: install | start | stop | status | uninstall | print-join | doctor
Run with --dry-run to print what would happen without touching the system.
"""
from __future__ import annotations

import argparse
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
HIVE_HOME = Path(os.path.expanduser("~/.hive"))
WINSW_URL = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"

DEFAULTS = {"queue_port": 5001, "git_port": 8418}

# Service ids (overridable with --name-prefix so a test install can't clobber a
# production one on the same box).
SVC = {"queue": "hive-queue-server", "git": "hive-git-http", "worker": "hive-worker-hal"}


def log(msg: str) -> None:
    print(f"[hostctl] {msg}")


def die(msg: str, code: int = 1):
    print(f"[hostctl] ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


# ── environment / detection ─────────────────────────────────────────────────

def os_kind() -> str:
    s = platform.system().lower()
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "macos"
    return "linux"


def local_ip() -> str:
    """Best-effort primary LAN IP (the address workers will dial back on)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.100.1", 9))  # no packet is actually sent
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def require(tool: str) -> str:
    p = shutil.which(tool)
    if not p:
        die(f"'{tool}' not found on PATH — install it and re-run")
    return p


def python_exe() -> str:
    return sys.executable or require("python3")


def _find_copilot() -> str:
    """Absolute path to the copilot CLI (services run with a minimal PATH)."""
    for name in ("copilot", "copilot.cmd", "copilot.exe"):
        p = shutil.which(name)
        if p:
            return p
    for cand in ("~/.local/bin/copilot", "C:/npm-global/copilot.cmd",
                 "/opt/homebrew/bin/copilot", "/usr/local/bin/copilot"):
        p = os.path.expanduser(cand)
        if os.path.isfile(p):
            return p
    return ""


# ── tokens + env file ───────────────────────────────────────────────────────

def ensure_tokens() -> tuple[str, str]:
    HIVE_HOME.mkdir(parents=True, exist_ok=True)
    ctl_f, ro_f = HIVE_HOME / "worker.token", HIVE_HOME / "readonly.token"
    for f in (ctl_f, ro_f):
        if not f.exists():
            f.write_text(secrets.token_hex(24) + "\n")
            try:
                os.chmod(f, 0o600)
            except OSError:
                pass
            log(f"generated {f.name}")
    return ctl_f.read_text().strip(), ro_f.read_text().strip()


def merge_env_file(updates: dict) -> None:
    """Set/override KEY=VALUE in ~/.hive/hive.env without clobbering other keys."""
    env_f = HIVE_HOME / "hive.env"
    lines = env_f.read_text().splitlines() if env_f.exists() else []
    keys = set(updates)
    out, seen = [], set()
    for ln in lines:
        k = ln.split("=", 1)[0].strip() if "=" in ln and not ln.lstrip().startswith("#") else None
        if k in keys:
            out.append(f"{k}={updates[k]}")
            seen.add(k)
        else:
            out.append(ln)
    for k in updates:
        if k not in seen:
            out.append(f"{k}={updates[k]}")
    env_f.write_text("\n".join(out) + "\n")
    try:
        os.chmod(env_f, 0o600)
    except OSError:
        pass


def service_env(kind: str, ctl: str, ro: str, cfg: dict) -> dict:
    """Env vars each control-plane service needs, independent of OS."""
    state = str(cfg["state_dir"])
    common = {"PYTHONUNBUFFERED": "1", "HIVE_DIR": state}
    qport, gport = cfg["queue_port"], cfg["git_port"]
    if kind == "queue":
        e = {**common, "HIVE_TOKEN": ctl, "HIVE_RO_TOKEN": ro,
             "HIVE_PORT": str(qport), "HIVE_BIND": "0.0.0.0"}
        if cfg["verify_optional"]:
            e["HIVE_VERIFY_OPTIONAL"] = "1"
        return e
    if kind == "git":
        return {**common, "HIVE_TOKEN": ctl, "GIT_HTTP_PORT": str(gport),
                "GIT_HTTP_BIND": "0.0.0.0", "GIT_PROJECT_ROOT": state}
    if kind == "worker":
        e = {**common, "HIVE_TOKEN": ctl,
             "HIVE_QUEUE_URL": f"http://127.0.0.1:{qport}",
             "HIVE_LAB_GIT": f"http://127.0.0.1:{gport}/lab.git"}
        cop = _find_copilot()
        if cop:
            e["HIVE_COPILOT_CMD"] = cop  # service PATH omits ~/.local/bin etc.
        return e
    raise ValueError(kind)


def service_argv(kind: str, py: str, cfg: dict) -> list:
    if kind == "queue":
        return [py, str(HERE / "queue_server.py")]
    if kind == "git":
        return [py, str(HERE / "git_http_server.py")]
    if kind == "worker":
        return [py, str(HERE / "worker_loop.py"), "--worker-id", "hal#1",
                "--lane", "hal", "--queue", f"http://127.0.0.1:{cfg['queue_port']}"]
    raise ValueError(kind)


# ── seed queue.db + lab.git (OS-agnostic, via hive_queue) ────────────────────

def seed_state(cfg: dict, dry: bool = False) -> None:
    if dry:
        log(f"[dry-run] would seed queue.db + lab.git under {cfg['state_dir']}")
        return
    sys.path.insert(0, str(HERE))
    os.environ["HIVE_DIR"] = str(cfg["state_dir"])
    Path(cfg["state_dir"]).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HIVE_LAB_GIT",
                          f"http://127.0.0.1:{cfg['git_port']}/lab.git")
    import hive_queue as q  # noqa: E402
    q.init_db()
    q.ensure_lab_repo()
    log(f"queue.db + lab.git seeded under {cfg['state_dir']}")


# ── per-OS service managers ──────────────────────────────────────────────────

def kinds(cfg: dict) -> list:
    return ["queue", "git"] + (["worker"] if cfg["with_worker"] else [])


def svc_id(kind: str, cfg: dict) -> str:
    return cfg["name_prefix"] + SVC[kind]


def _run(argv, dry, **kw):
    log("$ " + " ".join(str(a) for a in argv))
    if dry:
        return None
    return subprocess.run(argv, **kw)


# --- Linux: systemd --user ---------------------------------------------------

def _systemd_unit(kind: str, py: str, ctl: str, ro: str, cfg: dict) -> str:
    env = service_env(kind, ctl, ro, cfg)
    env_lines = "\n".join(f"Environment={k}={v}" for k, v in env.items())
    argv = " ".join(_sh_quote(a) for a in service_argv(kind, py, cfg))
    pre = ""
    if kind == "git":
        pre = f"ExecStartPre=/usr/bin/touch {Path(cfg['state_dir']) / 'lab.git' / 'git-daemon-export-ok'}\n"
    desc = {"queue": "queue control plane", "git": "lab.git smart-HTTP transport",
            "worker": "local worker (lane hal)"}[kind]
    return (
        "[Unit]\n"
        f"Description=HAL Hive — {desc}\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"WorkingDirectory={HERE}\n"
        f"EnvironmentFile=-{HIVE_HOME / 'hive.env'}\n"
        f"{env_lines}\n{pre}"
        f"ExecStart={argv}\n"
        "Restart=on-failure\nRestartSec=3\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def linux_install(py, ctl, ro, cfg, dry):
    udir = Path(os.path.expanduser("~/.config/systemd/user"))
    if not dry:
        udir.mkdir(parents=True, exist_ok=True)
    for k in kinds(cfg):
        unit = udir / f"{svc_id(k, cfg)}.service"
        text = _systemd_unit(k, py, ctl, ro, cfg)
        log(f"write {unit}")
        if not dry:
            unit.write_text(text)
    _run(["loginctl", "enable-linger", os.getlogin() if hasattr(os, "getlogin") else os.environ.get("USER", "")], dry, check=False)
    _run(["systemctl", "--user", "daemon-reload"], dry, check=False)
    for k in kinds(cfg):
        _run(["systemctl", "--user", "enable", "--now", svc_id(k, cfg)], dry, check=False)


def linux_ctl(action, cfg, dry):
    for k in kinds(cfg):
        _run(["systemctl", "--user", action, svc_id(k, cfg)], dry, check=False)


def linux_uninstall(cfg, dry):
    udir = Path(os.path.expanduser("~/.config/systemd/user"))
    for k in kinds(cfg):
        _run(["systemctl", "--user", "disable", "--now", svc_id(k, cfg)], dry, check=False)
        f = udir / f"{svc_id(k, cfg)}.service"
        log(f"rm {f}")
        if not dry and f.exists():
            f.unlink()
    _run(["systemctl", "--user", "daemon-reload"], dry, check=False)


# --- macOS: launchd ----------------------------------------------------------

def _plist(kind, py, ctl, ro, cfg) -> str:
    label = f"com.hal.{svc_id(kind, cfg)}"
    env = service_env(kind, ctl, ro, cfg)
    args = "".join(f"    <string>{_xml(a)}</string>\n" for a in service_argv(kind, py, cfg))
    envd = "".join(f"    <key>{k}</key><string>{_xml(v)}</string>\n" for k, v in env.items())
    logf = HIVE_HOME / f"{svc_id(kind, cfg)}.log"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f"  <key>Label</key><string>{label}</string>\n"
        f"  <key>ProgramArguments</key><array>\n{args}  </array>\n"
        f"  <key>WorkingDirectory</key><string>{HERE}</string>\n"
        f"  <key>EnvironmentVariables</key><dict>\n{envd}  </dict>\n"
        "  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n"
        f"  <key>StandardOutPath</key><string>{logf}</string>\n"
        f"  <key>StandardErrorPath</key><string>{logf}</string>\n"
        "</dict></plist>\n"
    )


def macos_install(py, ctl, ro, cfg, dry):
    ladir = Path(os.path.expanduser("~/Library/LaunchAgents"))
    if not dry:
        ladir.mkdir(parents=True, exist_ok=True)
    if cfg["with_worker"] is False:
        pass
    for k in kinds(cfg):
        pl = ladir / f"com.hal.{svc_id(k, cfg)}.plist"
        log(f"write {pl}")
        if not dry:
            pl.write_text(_plist(k, py, ctl, ro, cfg))
        _run(["launchctl", "unload", str(pl)], dry, check=False)
        _run(["launchctl", "load", str(pl)], dry, check=False)


def macos_ctl(action, cfg, dry):
    ladir = Path(os.path.expanduser("~/Library/LaunchAgents"))
    verb = {"start": "load", "stop": "unload", "restart": "kickstart"}.get(action, action)
    for k in kinds(cfg):
        pl = ladir / f"com.hal.{svc_id(k, cfg)}.plist"
        _run(["launchctl", verb, str(pl)], dry, check=False)


def macos_uninstall(cfg, dry):
    ladir = Path(os.path.expanduser("~/Library/LaunchAgents"))
    for k in kinds(cfg):
        pl = ladir / f"com.hal.{svc_id(k, cfg)}.plist"
        _run(["launchctl", "unload", str(pl)], dry, check=False)
        log(f"rm {pl}")
        if not dry and pl.exists():
            pl.unlink()


# --- Windows: WinSW ----------------------------------------------------------

def _fetch_winsw(dry) -> Path:
    dest = HERE / "winsw.exe"
    if dest.exists():
        return dest
    log(f"download WinSW <- {WINSW_URL}")
    if not dry:
        urllib.request.urlretrieve(WINSW_URL, dest)
    return dest


def _winsw_xml(kind, py, ctl, ro, cfg) -> str:
    sid = svc_id(kind, cfg)
    env = service_env(kind, ctl, ro, cfg)
    envx = "".join(f'  <env name="{k}" value="{_xml(v)}"/>\n' for k, v in env.items())
    argv = service_argv(kind, py, cfg)
    exe, args = argv[0], " ".join(_xml(a) for a in argv[1:])
    desc = {"queue": "queue control plane", "git": "lab.git smart-HTTP transport",
            "worker": "local worker (lane hal)"}[kind]
    # Runs as LocalSystem (no serviceaccount block) — no stored password.
    return (
        "<service>\n"
        f"  <id>{sid}</id>\n"
        f"  <name>HAL Hive — {desc}</name>\n"
        f"  <description>HAL hive control-plane component ({kind}).</description>\n"
        f"  <executable>{_xml(exe)}</executable>\n"
        f"  <arguments>{args}</arguments>\n"
        f"  <workingdirectory>{_xml(str(HERE))}</workingdirectory>\n"
        f"{envx}"
        "  <startmode>Automatic</startmode>\n"
        '  <onfailure action="restart" delay="5 sec"/>\n'
        "  <resetfailure>1 hour</resetfailure>\n"
        '  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold>'
        "<keepFiles>5</keepFiles></log>\n"
        "</service>\n"
    )


def windows_install(py, ctl, ro, cfg, dry):
    winsw = _fetch_winsw(dry)
    for k in kinds(cfg):
        sid = svc_id(k, cfg)
        exe = HERE / f"{sid}.exe"
        xml = HERE / f"{sid}.xml"
        log(f"write {xml}")
        if not dry:
            xml.write_text(_winsw_xml(k, py, ctl, ro, cfg))
            shutil.copyfile(winsw, exe)
        _run([str(exe), "stop"], dry, check=False)
        _run([str(exe), "uninstall"], dry, check=False)
        _run([str(exe), "install"], dry, check=False)
        _run([str(exe), "start"], dry, check=False)


def windows_ctl(action, cfg, dry):
    verb = {"restart": "restart"}.get(action, action)
    for k in kinds(cfg):
        exe = HERE / f"{svc_id(k, cfg)}.exe"
        _run([str(exe), verb], dry, check=False)


def windows_uninstall(cfg, dry):
    for k in kinds(cfg):
        exe = HERE / f"{svc_id(k, cfg)}.exe"
        _run([str(exe), "stop"], dry, check=False)
        _run([str(exe), "uninstall"], dry, check=False)


# ── small helpers ────────────────────────────────────────────────────────────

def _xml(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _sh_quote(s: str) -> str:
    s = str(s)
    if s and all(c.isalnum() or c in "-_./:=@" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


DISPATCH = {
    "linux": (linux_install, linux_ctl, linux_uninstall),
    "macos": (macos_install, macos_ctl, macos_uninstall),
    "windows": (windows_install, windows_ctl, windows_uninstall),
}


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_install(cfg, dry):
    okind = os_kind()
    py = python_exe()
    require("git")
    log(f"os={okind}  python={py}")
    ctl, ro = ensure_tokens()
    merge_env_file({
        "HIVE_TOKEN": ctl, "HIVE_RO_TOKEN": ro,
        "HIVE_PORT": str(cfg["queue_port"]),
        "HIVE_QUEUE_URL": f"http://127.0.0.1:{cfg['queue_port']}",
        "HIVE_LAB_GIT": f"http://127.0.0.1:{cfg['git_port']}/lab.git",
        **({"HIVE_VERIFY_OPTIONAL": "1"} if cfg["verify_optional"] else {}),
    })
    seed_state(cfg, dry)
    installer = DISPATCH[okind][0]
    installer(py, ctl, ro, cfg, dry)
    print()
    cmd_print_join(cfg, dry)


def cmd_ctl(action, cfg, dry):
    DISPATCH[os_kind()][1](action, cfg, dry)


def cmd_uninstall(cfg, dry):
    DISPATCH[os_kind()][2](cfg, dry)


def cmd_status(cfg, dry):
    ip = local_ip()
    print(f"dashboard : http://{ip}:{cfg['queue_port']}/")
    print(f"git xport : http://{ip}:{cfg['git_port']}/lab.git")
    okind = os_kind()
    for k in kinds(cfg):
        sid = svc_id(k, cfg)
        if okind == "linux":
            r = subprocess.run(["systemctl", "--user", "is-active", sid],
                               capture_output=True, text=True)
            print(f"  {sid:<28} {r.stdout.strip() or r.stderr.strip()}")
        else:
            print(f"  {sid:<28} (query via the platform service manager)")


def cmd_print_join(cfg, dry):
    ip = local_ip()
    base = f"http://{ip}:{cfg['queue_port']}"
    print("Hive host is up. Dashboard:", base + "/")
    print("Add a worker from another box (fill in <lane>/<caps>):")
    print(f"  Linux/macOS: curl -fsSL '{base}/bootstrap.sh?lane=<lane>&caps=<caps>' | sh")
    print(f"  Windows PS : powershell -NoProfile -ExecutionPolicy Bypass "
          f"-Command \"irm '{base}/bootstrap.ps1?lane=<lane>&caps=<caps>' | iex\"")


def cmd_doctor(cfg, dry):
    print("os_kind    :", os_kind())
    print("python     :", python_exe())
    print("git        :", shutil.which("git") or "MISSING")
    sys.path.insert(0, str(HERE))
    try:
        import git_http_server as g
        print("git-http-backend:", g.GIT_HTTP_BACKEND,
              "(exists)" if os.path.isfile(g.GIT_HTTP_BACKEND) else "(NOT FOUND)")
    except Exception as e:  # noqa: BLE001
        print("git-http-backend: <import failed>", e)
    print("local_ip   :", local_ip())
    print("hive_home  :", HIVE_HOME)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Stand up / manage a HAL hive control-plane host.")
    ap.add_argument("command",
                    choices=["install", "start", "stop", "restart", "status",
                             "uninstall", "print-join", "doctor"])
    ap.add_argument("--queue-port", type=int, default=DEFAULTS["queue_port"])
    ap.add_argument("--git-port", type=int, default=DEFAULTS["git_port"])
    ap.add_argument("--no-worker", action="store_true",
                    help="do not install a local worker on lane 'hal'")
    ap.add_argument("--verify-optional", action="store_true",
                    help="opt out of the fail-closed verify gate (lab hosts)")
    ap.add_argument("--name-prefix", default="",
                    help="prefix service ids (isolates a test install)")
    ap.add_argument("--state-dir", default=str(HERE),
                    help="where queue.db + lab.git live (default: the code dir)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print planned actions without changing the system")
    a = ap.parse_args(argv)
    cfg = {"queue_port": a.queue_port, "git_port": a.git_port,
           "with_worker": not a.no_worker, "verify_optional": a.verify_optional,
           "name_prefix": a.name_prefix,
           "state_dir": os.path.abspath(os.path.expanduser(a.state_dir))}
    if a.command == "install":
        cmd_install(cfg, a.dry_run)
    elif a.command in ("start", "stop", "restart"):
        cmd_ctl(a.command, cfg, a.dry_run)
    elif a.command == "uninstall":
        cmd_uninstall(cfg, a.dry_run)
    elif a.command == "status":
        cmd_status(cfg, a.dry_run)
    elif a.command == "print-join":
        cmd_print_join(cfg, a.dry_run)
    elif a.command == "doctor":
        cmd_doctor(cfg, a.dry_run)


if __name__ == "__main__":
    main()
