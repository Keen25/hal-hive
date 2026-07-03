#!/usr/bin/env python3
"""HAL hive — self-service worker bootstrap generator.

Produces a platform-appropriate setup script that a *fresh* worker box runs
once to join the hive. The operator copies a one-liner off the dashboard, runs
it on the new machine, and the script:

  1. resolves (or installs) Python 3.10+ and the `requests` dep,
  2. downloads the worker code from HAL (`/download/*`),
  3. plants the copilot auth token + queue token as service env,
  4. installs a persistent service (WinSW / systemd / launchd) that runs
     `worker_loop.py` on the given lane with the given caps,
  5. starts it — the worker then shows up on the dashboard.

Nothing here opens queue.db; workers only ever speak REST + smart-HTTP git, so
the same bootstrap works on Windows, Linux and macOS. Secrets (queue token,
copilot GH token) are injected server-side from HAL config, never checked in.

`build_script(os_kind, lane, caps, wid, token)` returns the script text;
`config()` gathers the HAL-side URLs + the cached copilot token.
"""
from __future__ import annotations

import os
import shlex

HAL_IP_DEFAULT = '192.168.100.1'
COPILOT_TOKEN_FILE = os.path.expanduser('~/.hive/copilot.token')


def _copilot_token() -> str:
    """Shared GH OAuth token that authenticates copilot on every worker."""
    try:
        with open(COPILOT_TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ''


def config() -> dict:
    """HAL-side endpoints + secrets baked into a bootstrap script.

    Overridable via env so HAL's LAN address isn't hard-coded:
      HIVE_HAL_IP        — HAL address workers dial back on (default .1)
      HIVE_PUBLIC_URL    — queue/control-plane base   (default http://<ip>:5001)
      HIVE_LAB_GIT_URL   — smart-HTTP git base        (default http://<ip>:8418/lab.git)
    """
    ip = os.environ.get('HIVE_HAL_IP', HAL_IP_DEFAULT)
    return {
        'queue': os.environ.get('HIVE_PUBLIC_URL', f'http://{ip}:5001'),
        'git':   os.environ.get('HIVE_LAB_GIT_URL', f'http://{ip}:8418/lab.git'),
        'ghtok': _copilot_token(),
        'py_url': os.environ.get(
            'HIVE_PY_URL',
            'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe'),
    }


def _caps_list(caps: str) -> list[str]:
    return [c.strip().lower() for c in caps.replace(',', ' ').split() if c.strip()]


def build_script(os_kind: str, lane: str, caps: str, wid: str, token: str) -> str:
    os_kind = (os_kind or 'windows').lower()
    lane = lane.strip()
    wid = (wid or f'{lane}#1').strip()
    cfg = config()
    if os_kind in ('windows', 'win', 'ps1'):
        return _windows(lane, _caps_list(caps), wid, token, cfg)
    return _unix(lane, _caps_list(caps), wid, token, cfg)


# ── Windows (PowerShell + WinSW) ────────────────────────────────────────────

def _windows(lane, caps, wid, token, cfg) -> str:
    cap_args = ' '.join(f'--cap {c}' for c in caps)
    q, g, ght, pyurl = cfg['queue'], cfg['git'], cfg['ghtok'], cfg['py_url']
    return f'''#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$QUEUE = '{q}'; $GIT = '{g}'; $TOKEN = '{token}'; $GHTOK = '{ght}'
$LANE = '{lane}'; $WID = '{wid}'; $DIR = 'C:\\hive'
Write-Host "== HAL hive worker bootstrap  lane=$LANE  id=$WID  caps={' '.join(caps) or '(auto)'} =="
New-Item -ItemType Directory -Force $DIR | Out-Null

function Test-Py($exe) {{
  try {{ $v = & $exe -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]'3.10') {{ return $v }} }} catch {{}}
  return $null
}}
$py = $null
foreach ($c in @("C:\\Program Files\\Python312\\python.exe",
                 "$env:LOCALAPPDATA\\Programs\\Python\\Python312\\python.exe",
                 "py","python")) {{
  $v = Test-Py $c; if ($v) {{ $py = $c; Write-Host "python $v -> $c"; break }}
}}
if (-not $py) {{
  Write-Host "installing Python 3.12 (all users)..."
  $ins = "$env:TEMP\\py312.exe"
  Invoke-WebRequest '{pyurl}' -OutFile $ins -UseBasicParsing
  Start-Process $ins -ArgumentList '/quiet','InstallAllUsers=1','PrependPath=1','Include_pip=1','TargetDir=C:\\Program Files\\Python312' -Wait
  $py = "C:\\Program Files\\Python312\\python.exe"
  if (-not (Test-Py $py)) {{ throw "Python install failed" }}
}}
& $py -m pip install --quiet --disable-pip-version-check requests | Out-Null

foreach ($f in 'worker_loop.py','hive_caps.py','hive_queue.py') {{
  Invoke-WebRequest "$QUEUE/download/$f" -OutFile "$DIR\\$f" -UseBasicParsing
}}
$winsw = "$DIR\\hive-worker-$LANE.exe"
if (-not (Test-Path $winsw)) {{ Invoke-WebRequest "$QUEUE/download/winsw.exe" -OutFile $winsw -UseBasicParsing }}

# copilot auth (machine env so the service account inherits it)
[Environment]::SetEnvironmentVariable('GH_TOKEN', $GHTOK, 'Machine')
[Environment]::SetEnvironmentVariable('GITHUB_TOKEN', $GHTOK, 'Machine')

# copilot CLI present?
$copilot = (Get-Command copilot.cmd -EA SilentlyContinue).Source
if (-not $copilot) {{
  $cand = 'C:\\npm-global\\copilot.cmd'
  if (Test-Path $cand) {{ $copilot = $cand }}
  elseif (Get-Command npm -EA SilentlyContinue) {{
    Write-Host "installing @github/copilot ..."; npm install -g @github/copilot 2>&1 | Out-Null
    $copilot = (Get-Command copilot.cmd -EA SilentlyContinue).Source
  }}
}}
if (-not $copilot) {{ Write-Warning "copilot CLI not found — install it, then restart hive-worker-$LANE" ; $copilot = 'C:\\npm-global\\copilot.cmd' }}

# svc-hive local admin (runs the service)
$u = 'svc-hive'; $p = ConvertTo-SecureString 'Passw0rd' -AsPlainText -Force
if (Get-LocalUser $u -EA SilentlyContinue) {{ Set-LocalUser $u -Password $p -PasswordNeverExpires $true }}
else {{ New-LocalUser $u -Password $p -FullName 'HAL Hive Service' -PasswordNeverExpires | Out-Null }}
if (-not ((Get-LocalGroupMember Administrators -EA SilentlyContinue).Name -match "\\\\$u$")) {{ Add-LocalGroupMember Administrators $u }}

$xml = @"
<service>
  <id>hive-worker-$LANE</id>
  <name>HAL Hive Worker ($LANE)</name>
  <description>Self-driving HAL hive worker. Lane $LANE.</description>
  <executable>$py</executable>
  <arguments>$DIR\\worker_loop.py --worker-id $WID --lane $LANE {cap_args}</arguments>
  <workingdirectory>$DIR</workingdirectory>
  <env name="PYTHONUNBUFFERED" value="1"/>
  <env name="HIVE_QUEUE_URL" value="$QUEUE"/>
  <env name="HIVE_TOKEN" value="$TOKEN"/>
  <env name="HIVE_LAB_GIT" value="$GIT"/>
  <env name="HIVE_COPILOT_CMD" value="$copilot"/>
  <startmode>Automatic</startmode>
  <onfailure action="restart" delay="10 sec"/>
  <resetfailure>1 hour</resetfailure>
  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>5</keepFiles></log>
  <serviceaccount>
    <username>.\\svc-hive</username>
    <password>Passw0rd</password>
    <allowservicelogonright>true</allowservicelogonright>
  </serviceaccount>
</service>
"@
Set-Content "$DIR\\hive-worker-$LANE.xml" $xml -Encoding UTF8

& $winsw stop 2>&1 | Out-Null
& $winsw uninstall 2>&1 | Out-Null
Start-Sleep 2
& $winsw install
& $winsw start
Start-Sleep 3
Get-Service "hive-worker-$LANE" | Format-List Name,Status,StartType
Write-Host "== done. dashboard: $QUEUE/ =="
'''


# ── Linux (systemd) / macOS (launchd) — one POSIX sh script ─────────────────

def _unix(lane, caps, wid, token, cfg) -> str:
    cap_args = ' '.join(f'--cap {shlex.quote(c)}' for c in caps)
    q, g, ght = cfg['queue'], cfg['git'], cfg['ghtok']
    return f'''#!/bin/sh
# HAL hive worker bootstrap (Linux/systemd or macOS/launchd)
set -eu
QUEUE='{q}'; GIT='{g}'; TOKEN='{token}'; GHTOK='{ght}'
LANE='{lane}'; WID='{wid}'; DIR="$HOME/.hive-worker"
echo "== HAL hive worker bootstrap  lane=$LANE  id=$WID  caps={' '.join(caps) or '(auto)'} =="
mkdir -p "$DIR"

# 1. python3 + requests
PY="$(command -v python3 || true)"
[ -n "$PY" ] || {{ echo "ERROR: python3 not found — install it and re-run"; exit 1; }}
"$PY" -m pip install --user --quiet requests 2>/dev/null || \\
  "$PY" -m pip install --quiet --break-system-packages requests 2>/dev/null || \\
  pip3 install --user --quiet requests 2>/dev/null || \\
  echo "WARN: could not auto-install requests; ensure it is importable"

# 2. worker code
for f in worker_loop.py hive_caps.py hive_queue.py; do
  curl -fsSL "$QUEUE/download/$f" -o "$DIR/$f"
done

# 3. copilot CLI present?
COPILOT="$(command -v copilot || true)"
if [ -z "$COPILOT" ] && command -v npm >/dev/null 2>&1; then
  echo "installing @github/copilot ..."; npm install -g @github/copilot >/dev/null 2>&1 || true
  COPILOT="$(command -v copilot || true)"
fi
[ -n "$COPILOT" ] || echo "WARN: copilot CLI not found — install it, then restart the service"

OS="$(uname -s)"
ARGS="$DIR/worker_loop.py --worker-id $WID --lane $LANE {cap_args}"

if [ "$OS" = "Linux" ] && command -v systemctl >/dev/null 2>&1; then
  # --- systemd (user unit; lingers so it runs without an active login) ---
  UDIR="$HOME/.config/systemd/user"; mkdir -p "$UDIR"
  cat > "$UDIR/hive-worker-$LANE.service" <<UNIT
[Unit]
Description=HAL Hive Worker ($LANE)
After=network-online.target

[Service]
ExecStart=$PY $ARGS
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
Environment=HIVE_QUEUE_URL=$QUEUE
Environment=HIVE_TOKEN=$TOKEN
Environment=HIVE_LAB_GIT=$GIT
Environment=GH_TOKEN=$GHTOK
Environment=GITHUB_TOKEN=$GHTOK
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
UNIT
  loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
  systemctl --user daemon-reload
  systemctl --user enable --now "hive-worker-$LANE.service"
  sleep 2; systemctl --user --no-pager status "hive-worker-$LANE.service" | head -5

elif [ "$OS" = "Darwin" ]; then
  # --- launchd (per-user LaunchAgent) ---
  PL="$HOME/Library/LaunchAgents/com.hal.hive-worker-$LANE.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  # ProgramArguments: python3 + each ARGS word as its own <string>
  PROGARGS="    <string>$PY</string>"
  for a in $ARGS; do PROGARGS="$PROGARGS
    <string>$a</string>"; done
  cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.hal.hive-worker-$LANE</string>
  <key>ProgramArguments</key><array>
$PROGARGS
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>HIVE_QUEUE_URL</key><string>$QUEUE</string>
    <key>HIVE_TOKEN</key><string>$TOKEN</string>
    <key>HIVE_LAB_GIT</key><string>$GIT</string>
    <key>GH_TOKEN</key><string>$GHTOK</string>
    <key>GITHUB_TOKEN</key><string>$GHTOK</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
PLIST
  launchctl unload "$PL" 2>/dev/null || true
  launchctl load "$PL"
  sleep 2; launchctl list | grep "hive-worker-$LANE" || true

else
  # --- fallback: nohup background (no service manager) ---
  echo "no systemd/launchd — starting under nohup (not boot-persistent)"
  PYTHONUNBUFFERED=1 HIVE_QUEUE_URL="$QUEUE" HIVE_TOKEN="$TOKEN" HIVE_LAB_GIT="$GIT" \\
    GH_TOKEN="$GHTOK" GITHUB_TOKEN="$GHTOK" \\
    nohup "$PY" $ARGS >"$DIR/worker.log" 2>&1 &
  echo "pid $!  log $DIR/worker.log"
fi
echo "== done. dashboard: $QUEUE/ =="
'''
