#!/usr/bin/env python3
"""HAL swarm — self-service worker bootstrap generator.

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
from xml.sax.saxutils import escape as _xml_escape

HAL_IP_DEFAULT = '192.168.100.1'
COPILOT_TOKEN_FILE = os.path.expanduser('~/.hive/copilot.token')


def _copilot_token() -> str:
    """Shared GH OAuth token that authenticates copilot on every worker."""
    try:
        with open(COPILOT_TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ''


def _tls_enabled() -> bool:
    """Is the control plane serving TLS? Mirrors queue_server/git_http_server:
    on iff both cert+key are configured. `HIVE_SCHEME=http|https` forces it so
    the generated scheme can be overridden independently of HAL's own sockets
    (e.g. when a reverse proxy terminates TLS)."""
    scheme = os.environ.get('HIVE_SCHEME', '').strip().lower()
    if scheme in ('http', 'https'):
        return scheme == 'https'
    return bool(os.environ.get('HIVE_TLS_CERT') and os.environ.get('HIVE_TLS_KEY'))


def _ca_pem() -> str:
    """The lab CA cert workers must trust to verify HAL over TLS. Path mirrors
    gen-lab-ca.sh: `$HIVE_TLS_CACERT`, else `$HIVE_CA_DIR/ca.crt`, else
    `~/.hive-ca/ca.crt`. Empty string if absent (falls back to no CA pinning)."""
    ca_dir = os.environ.get('HIVE_CA_DIR', os.path.expanduser('~/.hive-ca'))
    path = os.environ.get('HIVE_TLS_CACERT', os.path.join(ca_dir, 'ca.crt'))
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ''


def config() -> dict:
    """HAL-side endpoints + secrets baked into a bootstrap script.

    Overridable via env so HAL's LAN address isn't hard-coded:
      HIVE_HAL_IP        — HAL address workers dial back on (default .1)
      HIVE_PUBLIC_URL    — queue/control-plane base   (default <scheme>://<ip>:5001)
      HIVE_LAB_GIT_URL   — smart-HTTP git base        (default <scheme>://<ip>:8418/lab.git)

    Scheme follows HAL's TLS state (see `_tls_enabled`): when TLS is on, the
    generated worker URLs are https:// AND the lab CA PEM is embedded so the new
    worker trusts HAL without a manual trust step. This keeps "enable TLS" from
    silently stranding freshly-joined workers on http://."""
    ip = os.environ.get('HIVE_HAL_IP', HAL_IP_DEFAULT)
    tls = _tls_enabled()
    scheme = 'https' if tls else 'http'
    return {
        'queue': os.environ.get('HIVE_PUBLIC_URL', f'{scheme}://{ip}:5001'),
        'git':   os.environ.get('HIVE_LAB_GIT_URL', f'{scheme}://{ip}:8418/lab.git'),
        'ghtok': _copilot_token(),
        'tls':   tls,
        'ca_pem': _ca_pem() if tls else '',
        'py_url': os.environ.get(
            'HIVE_PY_URL',
            'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe'),
        # Per-task copilot subprocess ceiling (worker_loop COPILOT_TIMEOUT_S).
        # Its code default is 840s (14 min) — far too short for an ARS full-solution
        # build+test, which needs ~40 min+. Seed a generous 90 min so the timeout is
        # reproducible from bootstrap and a re-provision never silently reverts a
        # builder to 14 min. Override with HIVE_COPILOT_TIMEOUT_S.
        'copilot_timeout': os.environ.get('HIVE_COPILOT_TIMEOUT_S', '5400'),
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
    timeout = cfg['copilot_timeout']
    if cfg['tls'] and cfg['ca_pem']:
        # Import the lab CA into LocalMachine\\Root so IWR (downloads) and
        # git-over-schannel verify HAL; python-requests ignores the Windows
        # store, so also point REQUESTS_CA_BUNDLE at the on-disk PEM.
        ca_setup_ps = ("# TLS on: plant + trust the lab CA before any download\n"
                       "Set-Content \"$DIR\\ca.crt\" -Encoding ascii -Value @'\n"
                       + cfg['ca_pem'] + "\n'@\n"
                       "certutil -addstore -f Root \"$DIR\\ca.crt\" | Out-Null\n"
                       "[Environment]::SetEnvironmentVariable('REQUESTS_CA_BUNDLE', \"$DIR\\ca.crt\", 'Machine')\n")
        ca_env_winsw = '  <env name="REQUESTS_CA_BUNDLE" value="$DIR\\ca.crt"/>\n'
    else:
        ca_setup_ps = ''
        ca_env_winsw = ''
    return f'''#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$QUEUE = '{q}'; $GIT = '{g}'; $TOKEN = '{token}'; $GHTOK = '{ght}'
$LANE = '{lane}'; $WID = '{wid}'; $DIR = 'C:\\hive'; $TIMEOUT = '{timeout}'
Write-Host "== HAL swarm worker bootstrap  lane=$LANE  id=$WID  caps={' '.join(caps) or '(auto)'} =="
New-Item -ItemType Directory -Force $DIR | Out-Null
{ca_setup_ps}
function Test-Py($exe) {{
  try {{ $v = & $exe -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]'3.10') {{ return $v }} }} catch {{}}
  return $null
}}
$py = $null
$pyPaths = @("C:\\Program Files\\Python312\\python.exe",
             "$env:LOCALAPPDATA\\Programs\\Python\\Python312\\python.exe",
             "py","python")
foreach ($c in $pyPaths) {{
  $v = Test-Py $c; if ($v) {{ $py = $c; Write-Host "python $v -> $c"; break }}
}}
if (-not $py) {{
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  $ins = "$env:TEMP\\py312.exe"
  # All-users install needs elevation; fall back to a per-user install (no admin,
  # lands under %LOCALAPPDATA%) when the shell is not elevated so a fresh WORKGROUP
  # box still bootstraps.
  if ($admin) {{
    Write-Host "installing Python 3.12 (all users)..."
    $pyArgs = @('/quiet','InstallAllUsers=1','PrependPath=1','Include_pip=1','TargetDir=C:\\Program Files\\Python312')
  }} else {{
    Write-Host "installing Python 3.12 (current user — shell not elevated)..."
    $pyArgs = @('/quiet','InstallAllUsers=0','PrependPath=1','Include_pip=1')
  }}
  Invoke-WebRequest '{pyurl}' -OutFile $ins -UseBasicParsing
  $p = Start-Process $ins -ArgumentList $pyArgs -Wait -PassThru
  $rc = $p.ExitCode
  Write-Host "python installer exit code: $rc"
  # Re-probe every candidate path (per-user install lands under LOCALAPPDATA, not
  # the fixed all-users TargetDir), refreshing PATH so a freshly-added py/python resolves.
  $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
  foreach ($c in $pyPaths) {{ $v = Test-Py $c; if ($v) {{ $py = $c; Write-Host "python $v -> $c"; break }} }}
  if (-not $py -and (Get-Command winget -ErrorAction SilentlyContinue)) {{
    Write-Host "direct installer did not yield python (rc=$rc); trying winget..."
    winget install --id Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements --scope $(if ($admin) {{'machine'}} else {{'user'}}) 2>$null | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
    foreach ($c in $pyPaths) {{ $v = Test-Py $c; if ($v) {{ $py = $c; Write-Host "python $v -> $c"; break }} }}
  }}
  if (-not $py) {{ throw "Python install failed (installer rc=$rc). Install Python 3.10+ manually and re-run, or check the installer log at $env:TEMP\\Python*.log" }}
}}
& $py -m pip install --quiet --disable-pip-version-check requests | Out-Null

foreach ($f in 'worker_loop.py','hive_caps.py','hive_queue.py','hive_log.py','hive_version.py') {{
  Invoke-WebRequest "$QUEUE/download/$f" -OutFile "$DIR\\$f" -Headers @{{Authorization="Bearer $TOKEN"}} -UseBasicParsing
}}
$winsw = "$DIR\\hive-worker-$LANE.exe"
if (-not (Test-Path $winsw)) {{ Invoke-WebRequest "$QUEUE/download/winsw.exe" -OutFile $winsw -Headers @{{Authorization="Bearer $TOKEN"}} -UseBasicParsing }}

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

# MSBuild discovery -> machine PATH so the worker advertises build:msbuild.
# probe_caps() is which()-only (PATH-based, no subprocess); Visual Studio installs
# MSBuild OFF the PATH (e.g. D:\\VS2022\\MSBuild\\Current\\Bin\\MSBuild.exe), so a
# builder box would false-negative build:msbuild unless its dir is registered here.
# Non-fatal: a box without VS just skips this (no build:msbuild cap advertised).
try {{
  $msb = $null
  $vswhere = "${{env:ProgramFiles(x86)}}\\Microsoft Visual Studio\\Installer\\vswhere.exe"
  if (Test-Path $vswhere) {{
    $msb = & $vswhere -latest -products * -requires Microsoft.Component.MSBuild -find MSBuild\\**\\Bin\\MSBuild.exe 2>$null | Select-Object -First 1
  }}
  if (-not $msb) {{
    # vswhere absent or install unregistered (custom drive) — bounded disk probe.
    $roots = @('D:\\VS2022','D:\\Program Files\\Microsoft Visual Studio','D:\\Microsoft Visual Studio',
               'C:\\Program Files\\Microsoft Visual Studio','C:\\Program Files (x86)\\Microsoft Visual Studio')
    foreach ($root in $roots) {{
      if (Test-Path $root) {{
        $hit = Get-ChildItem -Path $root -Filter MSBuild.exe -Recurse -Depth 6 -ErrorAction SilentlyContinue |
               Where-Object {{ $_.FullName -match '\\\\Bin\\\\MSBuild\\.exe$' }} | Select-Object -First 1
        if ($hit) {{ $msb = $hit.FullName; break }}
      }}
    }}
  }}
  if ($msb) {{
    $msbDir = Split-Path $msb -Parent
    $machinePath = [Environment]::GetEnvironmentVariable('Path','Machine')
    if (($machinePath -split ';') -notcontains $msbDir) {{
      [Environment]::SetEnvironmentVariable('Path', ($machinePath.TrimEnd(';') + ';' + $msbDir), 'Machine')
      Write-Host "recorded MSBuild on machine PATH: $msbDir"
    }} else {{ Write-Host "MSBuild already on machine PATH: $msbDir" }}
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
  }} else {{ Write-Host "no MSBuild found (not a builder box) — skipping build:msbuild" }}
}} catch {{ Write-Warning "MSBuild discovery failed: $_" }}

# svc-hive service account (runs the worker service — LEAST PRIVILEGE, S3 #6).
# Generate a STRONG RANDOM password per box — never a hardcoded/known secret,
# never printed, never reused across machines. Lives only in the WinSW XML
# (admin-readable) as WinSW needs the plaintext for a custom service account.
$rb = New-Object 'System.Byte[]' 24
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($rb)
$PW = ([Convert]::ToBase64String($rb) -replace '[+/=]','x') + 'aA9!'
$u = 'svc-hive'; $p = ConvertTo-SecureString $PW -AsPlainText -Force
if (Get-LocalUser $u -EA SilentlyContinue) {{ Set-LocalUser $u -Password $p -PasswordNeverExpires $true }}
else {{ New-LocalUser $u -Password $p -FullName 'HAL Swarm Service' -PasswordNeverExpires | Out-Null }}
# S3 (#6): svc-hive runs as a STANDARD USER, never a local admin. A queued
# prompt is arbitrary code (copilot runs with broad tool access), so the service
# account must not be able to own the box. Undo Administrators from any earlier
# provision, then grant only what the worker needs.
if ((Get-LocalGroupMember Administrators -EA SilentlyContinue).Name -match "\\\\$u$") {{
  Remove-LocalGroupMember Administrators $u -EA SilentlyContinue
  Write-Host "removed $u from Administrators (least-privilege, S3 #6)"
}}
# Per-task sandbox: all clones/logs/prompts land under $DIR\\work (see the
# HIVE_WORK_DIR / TEMP env in the service XML below) so a task can only write
# where the worker explicitly contains it.
$WORK = "$DIR\\work"
New-Item -ItemType Directory -Force $WORK | Out-Null
# Grant svc-hive Modify on the code/token dir and Full control on the sandbox
# (inherited to children); no new access for anyone else.
icacls $DIR  /grant "${{u}}:(OI)(CI)M" | Out-Null
icacls $WORK /grant "${{u}}:(OI)(CI)F" | Out-Null
# copilot is installed under an npm-global dir the account may not read by
# default — grant read/execute so a non-admin service can still launch the CLI.
$copDir = Split-Path $copilot -Parent
if ($copDir -and (Test-Path $copDir)) {{ icacls $copDir /grant "${{u}}:(OI)(CI)RX" | Out-Null }}

$xml = @"
<service>
  <id>hive-worker-$LANE</id>
  <name>HAL Hive Worker ($LANE)</name>
  <description>Self-driving HAL hive worker. Lane $LANE.</description>
  <executable>$py</executable>
  <arguments>$DIR\\worker_loop.py --worker-id $WID --lane $LANE {cap_args}</arguments>
  <workingdirectory>$DIR</workingdirectory>
  <env name="PYTHONUNBUFFERED" value="1"/>
  <env name="COPILOT_TIMEOUT_S" value="$TIMEOUT"/>
  <env name="HIVE_QUEUE_URL" value="$QUEUE"/>
  <env name="HIVE_TOKEN" value="$TOKEN"/>
  <env name="HIVE_LAB_GIT" value="$GIT"/>
{ca_env_winsw}  <env name="HIVE_COPILOT_CMD" value="$copilot"/>
  <env name="HIVE_WORK_DIR" value="$WORK"/>
  <env name="TEMP" value="$WORK"/>
  <env name="TMP" value="$WORK"/>
  <startmode>Automatic</startmode>
  <onfailure action="restart" delay="10 sec"/>
  <resetfailure>1 hour</resetfailure>
  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>5</keepFiles></log>
  <serviceaccount>
    <username>.\\svc-hive</username>
    <password>$PW</password>
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
    # launchd ProgramArguments must be one <string> per argv token — build them
    # in Python so caps (or a $HOME/$DIR containing spaces) are never re-split by
    # the shell. $PY/$DIR/$WID/$LANE expand at runtime in the unquoted heredoc;
    # WID/LANE are charset-validated, caps are XML-escaped here.
    _prog = ['    <string>$PY</string>',
             '    <string>$DIR/worker_loop.py</string>',
             '    <string>--worker-id</string>', '    <string>$WID</string>',
             '    <string>--lane</string>', '    <string>$LANE</string>']
    for c in caps:
        _prog += ['    <string>--cap</string>',
                  f'    <string>{_xml_escape(c)}</string>']
    prog_args_xml = '\n'.join(_prog)
    q, g, ght = cfg['queue'], cfg['git'], cfg['ghtok']
    timeout = cfg['copilot_timeout']
    if cfg['tls'] and cfg['ca_pem']:
        # Plant the lab CA on the new box, then verify every fetch + all worker
        # traffic against it: requests honours REQUESTS_CA_BUNDLE and git honours
        # GIT_SSL_CAINFO, so no OS trust-store change (hence no root) is needed.
        ca_write_sh = ('# TLS on: plant the lab CA so downloads + worker traffic verify HAL\n'
                       'cat > "$DIR/ca.crt" <<\'CAEOF\'\n' + cfg['ca_pem'] + '\nCAEOF\n'
                       'CACERT="--cacert $DIR/ca.crt"\n')
        ca_env_systemd = ('Environment=REQUESTS_CA_BUNDLE=$DIR/ca.crt\n'
                          'Environment=GIT_SSL_CAINFO=$DIR/ca.crt\n')
        ca_env_plist = ('    <key>REQUESTS_CA_BUNDLE</key><string>$DIR/ca.crt</string>\n'
                        '    <key>GIT_SSL_CAINFO</key><string>$DIR/ca.crt</string>\n')
        ca_env_nohup = 'REQUESTS_CA_BUNDLE="$DIR/ca.crt" GIT_SSL_CAINFO="$DIR/ca.crt" '
    else:
        ca_write_sh = 'CACERT=""\n'
        ca_env_systemd = ca_env_plist = ca_env_nohup = ''
    return f'''#!/bin/sh
# HAL hive worker bootstrap (Linux/systemd or macOS/launchd)
set -eu
QUEUE='{q}'; GIT='{g}'; TOKEN='{token}'; GHTOK='{ght}'
LANE='{lane}'; WID='{wid}'; DIR="$HOME/.hive-worker"; TIMEOUT='{timeout}'
echo "== HAL swarm worker bootstrap  lane=$LANE  id=$WID  caps={' '.join(caps) or '(auto)'} =="
mkdir -p "$DIR"
{ca_write_sh}
# 1. python3 + requests
PY="$(command -v python3 || true)"
[ -n "$PY" ] || {{ echo "ERROR: python3 not found — install it and re-run"; exit 1; }}
"$PY" -m pip install --user --quiet requests 2>/dev/null || \\
  "$PY" -m pip install --quiet --break-system-packages requests 2>/dev/null || \\
  pip3 install --user --quiet requests 2>/dev/null || \\
  echo "WARN: could not auto-install requests; ensure it is importable"

# 2. worker code
for f in worker_loop.py hive_caps.py hive_queue.py hive_log.py hive_version.py; do
  curl -fsSL $CACERT -H "Authorization: Bearer $TOKEN" "$QUEUE/download/$f" -o "$DIR/$f"
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
Environment=COPILOT_TIMEOUT_S=$TIMEOUT
Environment=HIVE_QUEUE_URL=$QUEUE
Environment=HIVE_TOKEN=$TOKEN
Environment=HIVE_LAB_GIT=$GIT
Environment=GH_TOKEN=$GHTOK
Environment=GITHUB_TOKEN=$GHTOK
{ca_env_systemd}Restart=always
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
  cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.hal.hive-worker-$LANE</string>
  <key>ProgramArguments</key><array>
{prog_args_xml}
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>COPILOT_TIMEOUT_S</key><string>$TIMEOUT</string>
    <key>HIVE_QUEUE_URL</key><string>$QUEUE</string>
    <key>HIVE_TOKEN</key><string>$TOKEN</string>
    <key>HIVE_LAB_GIT</key><string>$GIT</string>
    <key>GH_TOKEN</key><string>$GHTOK</string>
    <key>GITHUB_TOKEN</key><string>$GHTOK</string>
{ca_env_plist}  </dict>
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
    {ca_env_nohup}nohup "$PY" $ARGS >"$DIR/worker.log" 2>&1 &
  echo "pid $!  log $DIR/worker.log"
fi
echo "== done. dashboard: $QUEUE/ =="
'''
