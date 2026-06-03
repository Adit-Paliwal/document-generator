# ══════════════════════════════════════════════════════════════════════════════
# IntelliDraft — Docker Deploy Script (Windows PowerShell)
# Usage: .\scripts\deploy.ps1
# Run from the repo root, or the script navigates there automatically.
# ══════════════════════════════════════════════════════════════════════════════

$ErrorActionPreference = "Stop"

# Navigate to repo root (parent of scripts/)
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host ""
Write-Host "  ╔══════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║     IntelliDraft  —  Docker Deploy   ║" -ForegroundColor Cyan
Write-Host "  ╚══════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# ── 1. Check .env ─────────────────────────────────────────────────────────────
if (-not (Test-Path "Data_Ingestion\.env")) {
    Write-Host "[!] Data_Ingestion\.env not found." -ForegroundColor Yellow
    Write-Host "    Copying .env.example → .env ..."
    Copy-Item "Data_Ingestion\.env.example" "Data_Ingestion\.env"
    Write-Host ""
    Write-Host "  ⚠  Edit Data_Ingestion\.env and set your API key:" -ForegroundColor Red
    Write-Host "     AZURE_GPT5_OPENAI_API_KEY=<your-key>" -ForegroundColor Red
    Write-Host "     AZURE_GPT5_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Then run this script again." -ForegroundColor Yellow
    exit 1
}

# ── 2. Check Docker is running ────────────────────────────────────────────────
try {
    docker info > $null 2>&1
} catch {
    Write-Host "[✗] Docker is not running. Start Docker Desktop and try again." -ForegroundColor Red
    exit 1
}

# ── 3. Build image ────────────────────────────────────────────────────────────
Write-Host "[1/3] Building Docker image..." -ForegroundColor Green
docker build -t intellidraft-api:latest .
Write-Host "      Image built." -ForegroundColor Green
Write-Host ""

# ── 4. Stop existing container ────────────────────────────────────────────────
Write-Host "[2/3] Stopping any existing container..." -ForegroundColor Green
try { docker-compose down --remove-orphans 2>$null } catch {}
Write-Host ""

# ── 5. Start ──────────────────────────────────────────────────────────────────
Write-Host "[3/3] Starting IntelliDraft API..." -ForegroundColor Green
docker-compose up -d
Write-Host ""

# ── 6. Wait for health ────────────────────────────────────────────────────────
Write-Host "Waiting for API to be ready..."
$max = 30; $count = 0; $ready = $false
while ($count -lt $max) {
    Start-Sleep -Seconds 2
    $count++
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:7071/api/templates" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
    Write-Host -NoNewline "."
}

Write-Host ""
Write-Host ""

if ($ready) {
    Write-Host "  ✅  IntelliDraft API is running!" -ForegroundColor Green
    Write-Host ""
    Write-Host "  API base URL  →  http://localhost:7071/api"
    Write-Host "  Health check  →  http://localhost:7071/api/templates"
    Write-Host ""
    Write-Host "  Useful commands:"
    Write-Host "    docker-compose logs -f                             # stream logs"
    Write-Host "    docker-compose down                                # stop"
    Write-Host "    docker-compose down -v                             # stop + wipe data"
    Write-Host "    docker-compose build; docker-compose up -d         # rebuild after changes"
} else {
    Write-Host "  [✗] API did not start in time." -ForegroundColor Red
    Write-Host "      Check logs:  docker-compose logs" -ForegroundColor Yellow
    exit 1
}
Write-Host ""
