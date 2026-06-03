#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
# IntelliDraft — Docker Deploy Script (Linux / macOS)
# Usage: bash scripts/deploy.sh
# ══════════════════════════════════════════════════════════════════════════════
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║     IntelliDraft  —  Docker Deploy   ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# ── 1. Check .env ─────────────────────────────────────────────────────────────
if [ ! -f "Data_Ingestion/.env" ]; then
    echo -e "${YELLOW}[!] Data_Ingestion/.env not found.${NC}"
    echo "    Copying .env.example → .env  (fill in your API keys before rerunning)"
    cp Data_Ingestion/.env.example Data_Ingestion/.env
    echo -e "${RED}    ⚠  Edit Data_Ingestion/.env and set your AZURE_GPT5_OPENAI_API_KEY,${NC}"
    echo -e "${RED}       then run this script again.${NC}"
    exit 1
fi

# ── 2. Check Docker is running ────────────────────────────────────────────────
if ! docker info > /dev/null 2>&1; then
    echo -e "${RED}[✗] Docker is not running. Start Docker Desktop and try again.${NC}"
    exit 1
fi

# ── 3. Build image ────────────────────────────────────────────────────────────
echo -e "${GREEN}[1/3] Building Docker image...${NC}"
docker build -t intellidraft-api:latest .
echo -e "${GREEN}      Image built successfully.${NC}"
echo ""

# ── 4. Stop existing container (if any) ──────────────────────────────────────
echo -e "${GREEN}[2/3] Stopping any existing container...${NC}"
docker-compose down --remove-orphans 2>/dev/null || true
echo ""

# ── 5. Start containers ───────────────────────────────────────────────────────
echo -e "${GREEN}[3/3] Starting IntelliDraft API...${NC}"
docker-compose up -d
echo ""

# ── 6. Wait for health check ──────────────────────────────────────────────────
echo "Waiting for API to be ready..."
MAX=30; COUNT=0
until curl -sf http://localhost:7071/api/templates > /dev/null 2>&1; do
    COUNT=$((COUNT+1))
    if [ $COUNT -ge $MAX ]; then
        echo -e "${RED}[✗] API did not start in time. Check logs: docker-compose logs${NC}"
        exit 1
    fi
    printf "."
    sleep 2
done

echo ""
echo ""
echo -e "${GREEN}  ✅  IntelliDraft API is running!${NC}"
echo ""
echo "  API base URL  →  http://localhost:7071/api"
echo "  Health check  →  http://localhost:7071/api/templates"
echo ""
echo "  Useful commands:"
echo "    docker-compose logs -f         # stream logs"
echo "    docker-compose down            # stop"
echo "    docker-compose down -v         # stop + wipe data"
echo "    docker-compose build && docker-compose up -d   # rebuild after code change"
echo ""
