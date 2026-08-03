# Culprit: one-command reproduction on Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\demo.ps1
#
# Equivalent to `make demo`. Each step is idempotent, so the script can be
# re-run after a failure without starting over.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"
$bin = Join-Path $venv "Scripts"

function Step($n, $msg) { Write-Host "`n=== [$n] $msg ===" -ForegroundColor Cyan }

Step 1 "Python environment"
if (-not (Test-Path $py)) {
    $launcher = if (Get-Command py -ErrorAction SilentlyContinue) { "py -V:3.12" } else { "python" }
    Invoke-Expression "$launcher -m venv `"$venv`""
}
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r (Join-Path $root "requirements.txt") --quiet
& $py -m pip install mcp-server-datahub --quiet

Step 2 "DataHub OSS (Docker). First run pulls several GB and takes 5-15 minutes."
& (Join-Path $bin "datahub.exe") docker quickstart

Step 3 "Real NYC TLC data into DuckDB (downloads ~250 MB)"
& $py (Join-Path $root "pipeline\load_raw.py")

Step 4 "dbt transforms"
$env:DBT_PROFILES_DIR = Join-Path $root "pipeline\dbt"
Push-Location (Join-Path $root "pipeline\dbt")
& (Join-Path $bin "dbt.exe") build
& (Join-Path $bin "dbt.exe") docs generate
Pop-Location

Step 5 "Ingest dbt lineage into DataHub"
Push-Location (Join-Path $root "pipeline")
& (Join-Path $bin "datahub.exe") ingest -c ingest_dbt.yml
Pop-Location

Step 6 "Train production model and counterfactual control"
& $py (Join-Path $root "pipeline\train_model.py")

Step 7 "Score the real 2025-06 month"
& $py (Join-Path $root "pipeline\score_batch.py") --month 2025-06

Step 8 "Emit ML lineage into DataHub"
& $py (Join-Path $root "pipeline\emit_ml_lineage.py")

Step 9 "Investigate"
if (-not $env:ANTHROPIC_API_KEY) {
    Write-Host "ANTHROPIC_API_KEY is not set, so the agent cannot run." -ForegroundColor Yellow
    Write-Host "Everything else is now standing up. Set the key and run:" -ForegroundColor Yellow
    Write-Host "  .venv\Scripts\python.exe -m culprit.cli investigate --write-back"
    Write-Host "`nDataHub UI: http://localhost:9002  (datahub / datahub)"
    exit 0
}
& $py -m culprit.cli investigate --write-back

Write-Host "`nDone. DataHub UI: http://localhost:9002  (datahub / datahub)" -ForegroundColor Green
