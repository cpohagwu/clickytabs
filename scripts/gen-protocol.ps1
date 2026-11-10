<#
.SYNOPSIS
    Regenerate the wire-protocol artifacts after changing backend/src/clicky/protocol/*.py.

.DESCRIPTION
    1. uv run --directory backend python -m clicky.protocol.export   -> backend/protocol.schema.json
    2. npm --prefix extension run gen:protocol                        -> extension/src/lib/protocol.gen.ts
    Stops at the first failing step and exits with its code. Windows PowerShell 5.1 compatible.

.EXAMPLE
    .\scripts\gen-protocol.ps1
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
# 'Continue': in Windows PowerShell 5.1, native stderr (uv/npm progress) can surface as error records when output
# is redirected; with 'Stop' that would abort a successful step. Failures are detected via $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    Write-Host '[1/2] Exporting backend/protocol.schema.json'
    uv run --directory backend python -m clicky.protocol.export
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Schema export failed (exit $LASTEXITCODE)." -ForegroundColor Red
        exit $LASTEXITCODE
    }

    Write-Host '[2/2] Generating extension/src/lib/protocol.gen.ts'
    npm --prefix extension run gen:protocol
    if ($LASTEXITCODE -ne 0) {
        Write-Host "TypeScript generation failed (exit $LASTEXITCODE)." -ForegroundColor Red
        exit $LASTEXITCODE
    }

    Write-Host 'Protocol artifacts are up to date. Commit both files together.'
}
finally {
    Pop-Location
}
