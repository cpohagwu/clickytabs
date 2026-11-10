<#
.SYNOPSIS
    Start the ClickyTabs dev loop: backend in a new PowerShell window, WXT dev server in this one.

.DESCRIPTION
    Runs `uv run --directory backend clicky serve` in a new window and `npm run dev` in extension/.
    Checks first that the backend venv (backend/.venv) and extension/node_modules exist, and prints the setup
    commands otherwise. Windows PowerShell 5.1 compatible.

.PARAMETER NoBackend
    Skip starting the backend (for example when it already runs elsewhere).

.EXAMPLE
    .\scripts\dev.ps1
#>
[CmdletBinding()]
param(
    [switch]$NoBackend
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $root 'backend'
$extensionDir = Join-Path $root 'extension'

function Test-Tool([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

$problems = @()
if (-not (Test-Tool 'uv')) { $problems += 'uv is not on PATH. Install it: winget install astral-sh.uv' }
if (-not (Test-Tool 'npm')) { $problems += 'npm is not on PATH. Install Node.js 22 or newer.' }
if (-not (Test-Path (Join-Path $backendDir '.venv'))) {
    $problems += 'Backend venv missing. Run: cd backend; uv sync --extra local'
}
if (-not (Test-Path (Join-Path $extensionDir 'node_modules'))) {
    $problems += 'Extension dependencies missing. Run: cd extension; npm ci'
}

if ($problems.Count -gt 0) {
    Write-Host 'ClickyTabs dev setup is incomplete:' -ForegroundColor Yellow
    foreach ($p in $problems) { Write-Host "  - $p" }
    Write-Host ''
    Write-Host 'Then run .\scripts\dev.ps1 again. See README.md (Quickstart).'
    exit 1
}

if (-not $NoBackend) {
    Write-Host 'Starting backend in a new window: uv run --directory backend clicky serve'
    # -WorkingDirectory avoids quoting the (possibly space-containing) repo path inside the child command line.
    Start-Process -FilePath 'powershell.exe' -WorkingDirectory $root -ArgumentList @(
        '-NoExit',
        '-NoProfile',
        '-Command',
        '$Host.UI.RawUI.WindowTitle = ''ClickyTabs backend''; uv run --directory backend clicky serve'
    ) | Out-Null
}

Write-Host 'Starting extension dev server: npm run dev (Ctrl+C to stop)'
Write-Host 'Pair in onboarding with ws://127.0.0.1:8765/v1/ws and the token from: uv run --directory backend clicky token show'
# Native stderr must not abort the dev server under 5.1 when output is redirected; rely on $LASTEXITCODE.
$ErrorActionPreference = 'Continue'
$code = 1
Push-Location $extensionDir
try {
    npm run dev
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $code
