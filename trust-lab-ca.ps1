<#
.SYNOPSIS
  Trust the HAL Hive lab CA on a Windows worker (e.g. ar1) so it can talk to the
  TLS-enabled hive over https:// with no cert errors. Windows-side counterpart of
  gen-lab-ca.sh (issue #8 / S5).

.DESCRIPTION
  Imports ca.crt into the LocalMachine\Root trust store (must run elevated), which
  makes .NET / PowerShell / curl.exe / git-for-windows all trust the hive server
  cert. Git for Windows uses its OWN CA bundle by default, so this script can also
  point git at the Windows system store (schannel) or append the CA to git's PEM
  bundle — pick with -GitTrust.

  Copy ca.crt from HAL first, e.g. from the KVM host:
    scp ~/.hive-ca/ca.crt <win-user>@ar1:C:\hive\ca.crt
  (or paste it via your existing WinRM push path).

.PARAMETER CaPath
  Path to ca.crt on the Windows box. Default: .\ca.crt

.PARAMETER GitTrust
  How to make Git for Windows trust the CA:
    schannel  - set http.sslBackend=schannel so git uses the Windows Root store
                (recommended; the LocalMachine\Root import then just works)
    pem       - append the CA to git's ca-bundle.crt (mingw OpenSSL backend)
    none      - only import into the Windows store, leave git config untouched

.EXAMPLE
  # from an elevated PowerShell on ar1:
  .\trust-lab-ca.ps1 -CaPath C:\hive\ca.crt -GitTrust schannel
#>
[CmdletBinding()]
param(
  [string]$CaPath = ".\ca.crt",
  [ValidateSet("schannel", "pem", "none")]
  [string]$GitTrust = "schannel"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $CaPath)) {
  throw "CA cert not found: $CaPath  (copy ~/.hive-ca/ca.crt from HAL first)"
}
$CaPath = (Resolve-Path $CaPath).Path

# --- elevation check --------------------------------------------------------
$admin = ([Security.Principal.WindowsPrincipal] `
  [Security.Principal.WindowsIdentity]::GetCurrent()
  ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $admin) {
  throw "Run this in an ELEVATED PowerShell — importing into LocalMachine\Root needs admin."
}

# --- import into the machine Root store -------------------------------------
# certutil is present on every Windows box and is the least-surprise path.
Write-Host "[ca] importing $CaPath into LocalMachine\Root ..."
& certutil.exe -addstore -f Root $CaPath | Out-Null
if ($LASTEXITCODE -ne 0) { throw "certutil -addstore failed (exit $LASTEXITCODE)" }

# Show the thumbprint so you can verify / remove later.
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 $CaPath
Write-Host ("[ca] trusted: {0}  thumbprint {1}" -f $cert.Subject, $cert.Thumbprint)
Write-Host "     remove later with: certutil -delstore Root $($cert.Thumbprint)"

# --- make Git for Windows trust it too --------------------------------------
switch ($GitTrust) {
  "schannel" {
    if (Get-Command git -ErrorAction SilentlyContinue) {
      git config --system http.sslBackend schannel
      Write-Host "[git] http.sslBackend=schannel (system) — git now uses the Windows Root store"
    } else {
      Write-Warning "[git] git not on PATH; skipped schannel config"
    }
  }
  "pem" {
    if (Get-Command git -ErrorAction SilentlyContinue) {
      $bundle = (git config --system --get http.sslCAInfo)
      if (-not $bundle) {
        $exe = (Get-Command git).Source                       # ...\cmd\git.exe
        $root = Split-Path (Split-Path $exe -Parent) -Parent  # git install root
        $bundle = Join-Path $root "mingw64\etc\ssl\certs\ca-bundle.crt"
      }
      if (Test-Path $bundle) {
        # Idempotent: only append if the CA isn't already in the bundle.
        $pem = Get-Content $CaPath -Raw
        if ((Get-Content $bundle -Raw) -notlike "*$($pem.Trim())*") {
          Add-Content -Path $bundle -Value "`n$pem"
          Write-Host "[git] appended CA to $bundle"
        } else {
          Write-Host "[git] CA already present in $bundle"
        }
      } else {
        Write-Warning "[git] ca-bundle.crt not found ($bundle); use -GitTrust schannel instead"
      }
    }
  }
  "none" { Write-Host "[git] left untouched (-GitTrust none)" }
}

Write-Host ""
Write-Host "Done. Point the worker at the hive over https, e.g.:"
Write-Host "  git remote set-url origin https://<HAL-ip>:8418/lab.git"
Write-Host "  set HIVE_QUEUE_URL=https://<HAL-ip>:5001"
