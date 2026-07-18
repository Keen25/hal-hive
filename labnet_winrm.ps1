#!/usr/bin/env pwsh
#Requires -Version 7.2

<#
labnet_winrm.ps1 — run a command inside a freshly-provisioned lab VM over WinRM.

Used by labctl to enroll a newly built box as a hive worker (it runs the queue
server's bootstrap.ps1 one-liner in-guest). Kept separate from labctl.py so the
credential resolution and the Negotiate/NTLM channel stay identical to
autofedora.ps1 (the proven provisioning engine) — same 3-tier cred order, same
`Invoke-Command -Authentication Negotiate` transport that needs gss-ntlmssp on
Linux.

Cred resolution order (identical to autofedora.ps1):
  1. AdminPassword in lab-config.psd1 (plaintext override; warns)
  2. encrypted cred file  admin.cred + admin.key  (AUTOFEDORA_CRED_DIR or ~/.config/autofedora)
  3. FAIL — this script is non-interactive by design (never prompts)

Exit codes: 0 ok · 2 bad args · 3 no credential · 4 remoting/command failure.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Ip,
    # The command to run in-guest. Runs as the local Administrator of the box.
    [Parameter(Mandatory)] [string] $Command,
    # Where lab-config.psd1 lives (defaults to the autofedora repo).
    [string] $AutofedoraDir = $(if ($env:AUTOFEDORA_DIR) { $env:AUTOFEDORA_DIR } else { Join-Path $HOME 'work/Toolz/autofedora' }),
    # How long to keep retrying the initial WinRM connect (guest may still be booting).
    [int] $ConnectTimeoutSec = 300
)

$ErrorActionPreference = 'Stop'

function Die([string]$msg, [int]$code) { Write-Error $msg; exit $code }

$cfgFile = Join-Path $AutofedoraDir 'lab-config.psd1'
if (-not (Test-Path $cfgFile)) { Die "lab-config.psd1 not found at $cfgFile (set -AutofedoraDir)" 2 }
$cfg = Import-PowerShellDataFile $cfgFile

# ── credential (same order as autofedora.ps1; never prompts) ────────────────
$credDir  = if ($env:AUTOFEDORA_CRED_DIR) { $env:AUTOFEDORA_CRED_DIR } else { Join-Path $HOME '.config/autofedora' }
$credFile = Join-Path $credDir 'admin.cred'
$keyFile  = Join-Path $credDir 'admin.key'

if (-not [string]::IsNullOrEmpty($cfg.AdminPassword)) {
    $secure = ConvertTo-SecureString $cfg.AdminPassword -AsPlainText -Force
} elseif ((Test-Path $credFile) -and (Test-Path $keyFile)) {
    $key = [System.IO.File]::ReadAllBytes($keyFile)
    if ($key.Length -ne 32) { Die "Bad AES key length in $keyFile (expected 32 bytes, got $($key.Length))." 3 }
    $enc = (Get-Content -Raw $credFile).Trim()
    $secure = ConvertTo-SecureString -String $enc -Key $key
} else {
    Die "No credential available (set AdminPassword in lab-config.psd1 or create the encrypted cred file via set-autofedora-cred.ps1)." 3
}
$cred = New-Object System.Management.Automation.PSCredential ('administrator', $secure)

# ── wait for WinRM, then run the command ────────────────────────────────────
$deadline = (Get-Date).AddSeconds($ConnectTimeoutSec)
$session  = $null
while ($null -eq $session -and (Get-Date) -lt $deadline) {
    try {
        $session = New-PSSession -ComputerName $Ip -Credential $cred -Authentication Negotiate -ErrorAction Stop
    } catch {
        Start-Sleep 10
    }
}
if ($null -eq $session) { Die "Could not open a WinRM session to $Ip within ${ConnectTimeoutSec}s." 4 }

try {
    $sb = [ScriptBlock]::Create($Command)
    Invoke-Command -Session $session -ScriptBlock $sb
    if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) { Die "In-guest command exited $LASTEXITCODE." 4 }
} finally {
    Remove-PSSession $session -ErrorAction SilentlyContinue
}
