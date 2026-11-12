$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'run.ps1')
exit $LASTEXITCODE
