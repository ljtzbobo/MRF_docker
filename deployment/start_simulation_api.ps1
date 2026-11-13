param([int]$Port = 8000)
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$env:MRF_DEPLOYMENT_CONFIG = Join-Path $projectRoot 'examples/ResourceSimulations/heterogeneous_simulation.json'
& (Join-Path $projectRoot '.venv/Scripts/python.exe') -m uvicorn backend.api.app:app --host 127.0.0.1 --port $Port
