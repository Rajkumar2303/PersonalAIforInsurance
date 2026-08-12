# ---------------------------------------------------------------------------
# Ontario All-Quote Agent - one-command demo startup (Issue #14)
#
# Verifies required local dependencies, starts the backend (FastAPI) and the
# frontend (Vite), and prints the URLs to open.
#
# DEMO REQUIRED:  Python venv + installed requirements, Node deps, Chromium
#                 (for the local mock browser agent).
# OPTIONAL/LIVE:  DATABASE_URL, LANGSMITH_API_KEY, LLM keys, telephony creds -
#                 NONE of these are needed for the demo; their absence must not
#                 block startup.
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Backend = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'
$Venv = Join-Path $Backend '.venv'
$Python = Join-Path $Venv 'Scripts\python.exe'

Write-Host "== Ontario All-Quote Agent - demo startup ==" -ForegroundColor Cyan
Write-Host "Repo: $Root`n"

# ---- 1. Verify local dependencies ---------------------------------------
if (-not (Test-Path $Venv)) {
    Write-Host "Creating Python virtualenv at $Venv ..." -ForegroundColor Yellow
    & python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create venv. Install Python 3.11+ and retry." }
}
if (-not (Test-Path $Python)) { throw "venv python not found: $Python" }

if (-not $SkipInstall) {
    Write-Host "Installing backend requirements (first run only)..." -ForegroundColor Yellow
    & $Python -m pip install --upgrade pip --quiet
    & $Python -m pip install -r (Join-Path $Backend 'requirements.txt') --quiet
    if ($LASTEXITCODE -ne 0) { throw "Backend dependency install failed." }
    & $Python -m pip install -r (Join-Path $Backend 'requirements-dev.txt') --quiet
}

if (-not (Test-Path (Join-Path $Frontend 'node_modules'))) {
    Write-Host "Installing frontend dependencies..." -ForegroundColor Yellow
    Push-Location $Frontend
    try {
        & npm install
        if ($LASTEXITCODE -ne 0) { throw "npm install failed." }
    } finally { Pop-Location }
}

# Playwright Chromium (headless mock browser). Skip check if already present
# via PLAYWRIGHT_BROWSERS_PATH; simplest is to attempt install only if missing.
Write-Host "Ensuring Playwright Chromium is available..." -ForegroundColor Yellow
& $Python -m playwright install chromium 2>$null | Out-Null

# ---- 2. Environment sanity check (demo never needs external credentials) --
Write-Host "`nEnvironment check: DEMO requires NO external credentials." -ForegroundColor Green
Write-Host "  Optional/LIVE (absent is fine for the demo):"
Write-Host "    DATABASE_URL, LANGSMITH_API_KEY, LLM keys, telephony creds"

# ---- 3. Start backend (FastAPI on :8000) ---------------------------------
Write-Host "`nStarting backend on http://localhost:8000 ..." -ForegroundColor Cyan
Push-Location $Backend
$backendJob = Start-Job -ScriptBlock {
    param($py, $cwd)
    Set-Location $cwd
    & $py -m uvicorn app.main:app --host 127.0.0.1 --port 8000
} -ArgumentList $Python, $Backend
Pop-Location

# ---- 4. Start frontend (Vite on :5173) -----------------------------------
Write-Host "Starting frontend on http://localhost:5173 ..." -ForegroundColor Cyan
Push-Location $Frontend
$frontendJob = Start-Job -ScriptBlock {
    param($cwd)
    Set-Location $cwd
    & npm run dev
} -ArgumentList $Frontend
Pop-Location

Write-Host "`n================================" -ForegroundColor Green
Write-Host "  OPEN: http://localhost:5173" -ForegroundColor Green
Write-Host "  Backend API: http://localhost:8000 (health: /health)" -ForegroundColor Green
Write-Host "  Env check:   http://localhost:8000/api/v1/demo/env" -ForegroundColor Green
Write-Host "================================" -ForegroundColor Green
Write-Host "`nDemo flow: Auto Insurance -> fill demo profile -> Review & Consent"
Write-Host "-> grant consent -> Compare Quotes -> polled multi-source results.`n"

Write-Host "Press Ctrl+C to stop both servers." -ForegroundColor Yellow
try {
    Wait-Job $backendJob, $frontendJob -Timeout 3600 | Out-Null
} finally {
    Stop-Job $backendJob, $frontendJob -ErrorAction SilentlyContinue
    Remove-Job $backendJob, $frontendJob -Force -ErrorAction SilentlyContinue
}
