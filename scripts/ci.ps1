<#
.SYNOPSIS
  Everything CI checks, in order, stopping at the first failure.

.DESCRIPTION
  Backend: locked install, lint, tests, protocol schema, worker lock. Extension: locked install, generated types,
  type check, unit tests, build, manifest, then the Playwright suite against `clicky serve` on the
  fake profile (no models, no keys). Run it from a clean checkout to reproduce CI:

    powershell -ExecutionPolicy Bypass -File scripts/ci.ps1             # everything (about 6 minutes)
    powershell -ExecutionPolicy Bypass -File scripts/ci.ps1 -SkipE2E    # without the browser tests

  .github/workflows/ci.yml runs the same steps on Linux.
#>
param([switch]$SkipE2E)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$total = Get-Date

function Step([string]$Name, [scriptblock]$Command) {
  Write-Host "==> $Name" -ForegroundColor Cyan
  $started = Get-Date
  & $Command
  if ($LASTEXITCODE -ne 0) {
    Write-Host "FAILED: $Name (exit $LASTEXITCODE)" -ForegroundColor Red
    exit $LASTEXITCODE
  }
  Write-Host ("    ok ({0:N0} s)" -f ((Get-Date) - $started).TotalSeconds)
}

Push-Location (Join-Path $root 'backend')
try {
  # Bytecode at install time: otherwise a fresh venv compiles (and the virus scanner reads) every module during the
  # first timed test, which then times out.
  Step 'backend: install (locked)' { uv sync --locked --compile-bytecode --extra local --extra kokoro --extra anthropic }
  Step 'backend: lint' { uv run --no-sync ruff check . }
  Step 'backend: tests' { uv run --no-sync pytest -q }
  Step 'backend: protocol schema is current' { uv run --no-sync python -m clicky.protocol.export --check }
  Step 'worker: lock file is current' { uv lock --check --directory automation-worker }
} finally {
  Pop-Location
}

Push-Location (Join-Path $root 'extension')
try {
  Step 'extension: install (locked)' { npm ci --no-audit --no-fund }
  Step 'extension: generated protocol types are current' { npm run gen:protocol:check }
  Step 'extension: type check' { npm run check }
  Step 'extension: unit tests' { npm run test }
  Step 'extension: build' { npm run build }
  Step 'extension: manifest' { npm run check:manifest }
  if (-not $SkipE2E) {
    Step 'e2e: bundled Chromium' { npx playwright install chromium }
    Step 'e2e: Playwright on the fake profile' { npx playwright test --reporter=line }
  }
} finally {
  Pop-Location
}

Write-Host ("CI passed in {0:N1} min." -f ((Get-Date) - $total).TotalMinutes) -ForegroundColor Green
