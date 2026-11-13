# Run the worker on this PC with its NVIDIA GPU, writing to the hosted database.
#
# Usage, from the repo root in Windows PowerShell:
#   .\worker\run_local.ps1
#
# It asks for the two secrets with hidden prompts (nothing is echoed or saved):
#   - the traffic_writer connection string (database/worker_writer_role.sql)
#   - the worker token that every /jobs request must send
# and serves the worker on http://127.0.0.1:7860 only, so nothing outside this PC can
# reach it. Stop it with Ctrl+C; the secrets go away with the process.

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Read-Secret([string]$prompt) {
    $secure = Read-Host $prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

$databaseUrl = Read-Secret 'traffic_writer connection string'
if ($databaseUrl -notlike 'postgresql://traffic_writer:*') {
    throw 'That is not a traffic_writer connection string. Never use the owner or traffic_api login here.'
}
$workerToken = Read-Secret 'Worker token'
if ($workerToken.Length -lt 32) { throw 'The worker token looks too short.' }

$env:DATABASE_URL = $databaseUrl
$env:WORKER_TOKEN = $workerToken
$env:DATA_DIR = Join-Path $repo 'data\worker'
$env:WORKER_DEVICE = '0'           # the RTX 3060
$env:WORKER_MODEL = 'yolo26m.pt'   # the pipeline's tested settings (pipeline/README.md)
$env:WORKER_IMGSZ = '1280'
# The 30-minute camera sweep, and the districts it covers. Empty means no sweep; 3 is
# Sacramento, which is where the map's heat starts. Add districts on purpose, not by default.
$env:WORKER_SWEEP_DISTRICTS = '3'
Remove-Variable databaseUrl, workerToken

try {
    Write-Host "Worker on http://127.0.0.1:7860 (GPU 0, yolo26m, 1280), sweeping district 3 every 30 min. Ctrl+C to stop."
    & "$repo\.venv\Scripts\python.exe" -m uvicorn worker.app:app --host 127.0.0.1 --port 7860
}
finally {
    Remove-Item Env:DATABASE_URL, Env:WORKER_TOKEN -ErrorAction SilentlyContinue
}
