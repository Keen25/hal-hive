#!/usr/bin/env pwsh
#Requires -Version 7.2
# ar1exec.ps1 — run a command in a lab VM as the box's LOCAL Administrator over
# WinRM and STREAM stdout/stderr back. Same 3-tier cred resolution as
# labnet_winrm.ps1 (local admin, so it works even when the domain secure channel
# is broken). Diagnostics/repair helper — NOT a replacement for the bootstrap path.
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Ip,
    [string] $Command,
    [string] $ScriptFile,
    [string] $AutofedoraDir = $(if ($env:AUTOFEDORA_DIR) { $env:AUTOFEDORA_DIR } else { Join-Path $HOME 'work/Toolz/autofedora' }),
    [int] $ConnectTimeoutSec = 180
)
$ErrorActionPreference = 'Stop'
function Die([string]$m,[int]$c){ Write-Error $m; exit $c }
if ($ScriptFile) {
    if (-not (Test-Path $ScriptFile)) { Die "ScriptFile not found: $ScriptFile" 2 }
    $Command = Get-Content -Raw -LiteralPath $ScriptFile
} elseif (-not $Command) { Die "Pass -Command or -ScriptFile." 2 }

$cfgFile = Join-Path $AutofedoraDir 'lab-config.psd1'
if (-not (Test-Path $cfgFile)) { Die "lab-config.psd1 not found at $cfgFile" 2 }
$cfg = Import-PowerShellDataFile $cfgFile

$credDir  = if ($env:AUTOFEDORA_CRED_DIR) { $env:AUTOFEDORA_CRED_DIR } else { Join-Path $HOME '.config/autofedora' }
$credFile = Join-Path $credDir 'admin.cred'
$keyFile  = Join-Path $credDir 'admin.key'
if (-not [string]::IsNullOrEmpty($cfg.AdminPassword)) {
    $secure = ConvertTo-SecureString $cfg.AdminPassword -AsPlainText -Force
} elseif ((Test-Path $credFile) -and (Test-Path $keyFile)) {
    $key = [System.IO.File]::ReadAllBytes($keyFile)
    if ($key.Length -ne 32) { Die "Bad AES key length in $keyFile" 3 }
    $enc = (Get-Content -Raw $credFile).Trim()
    $secure = ConvertTo-SecureString -String $enc -Key $key
} else { Die "No credential available." 3 }
$cred = New-Object System.Management.Automation.PSCredential ('administrator', $secure)

$deadline = (Get-Date).AddSeconds($ConnectTimeoutSec)
$session = $null
while ($null -eq $session -and (Get-Date) -lt $deadline) {
    try { $session = New-PSSession -ComputerName $Ip -Credential $cred -Authentication Negotiate -ErrorAction Stop }
    catch { Start-Sleep 8 }
}
if ($null -eq $session) { Die "Could not open WinRM session to $Ip in ${ConnectTimeoutSec}s." 4 }
try {
    $sb = [ScriptBlock]::Create($Command)
    Invoke-Command -Session $session -ScriptBlock $sb 2>&1 | Out-String -Width 200
} finally {
    Remove-PSSession $session -ErrorAction SilentlyContinue
}
