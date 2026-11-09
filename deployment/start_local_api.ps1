param([int]$Port = 8000)

$projectRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $PSScriptRoot 'config.local.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    throw '缺少本机画像，请先运行 python -m deployment.calibrate_local'
}
Set-Location -LiteralPath $projectRoot
$env:MRF_DEPLOYMENT_CONFIG = $configPath
python -m uvicorn backend.api.app:app --host 127.0.0.1 --port $Port
