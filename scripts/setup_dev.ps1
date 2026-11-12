$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$sources = Get-Content docs/sources.lock.json -Raw | ConvertFrom-Json
foreach ($entry in $sources.sources.PSObject.Properties) {
    $source = $entry.Value
    if (-not (Test-Path -LiteralPath $source.path)) {
        git clone $source.url $source.path
        if ($LASTEXITCODE -ne 0) { throw '來源下載失敗' }
        git -C $source.path checkout --detach $source.commit
        if ($LASTEXITCODE -ne 0) { throw '來源版本鎖定失敗' }
    }
    $actual = git -C $source.path rev-parse HEAD
    if ($actual -ne $source.commit) { throw ('來源版本不符：' + $entry.Name) }
}
uv sync --locked --python 3.12 --cache-dir .cache/uv
if ($LASTEXITCODE -ne 0) { throw 'Python 環境安裝失敗' }
& .venv/Scripts/python.exe scripts/setup_font.py
if ($LASTEXITCODE -ne 0) { throw '字型安裝失敗' }
Push-Location ntpu-ai-bridge
try {
    npm ci --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Bridge 環境安裝失敗' }
} finally { Pop-Location }
Write-Output '環境就緒。執行 ./scripts/run.ps1 開啟。'
