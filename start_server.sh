#!/usr/bin/env bash
# ============================================================
#  DAVAI — Start All Services
#  Web server: http://localhost:5050
#  App backend: http://localhost:3000
# ============================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
log() { echo -e "${CYAN}[DAVAI]${NC} $1"; }
ok()  { echo -e "${GREEN}[OK]${NC} $1"; }

echo ""
echo "========================================"
echo "  DAVAI — Starting Services"
echo "========================================"

# Kill existing
lsof -ti:5050 | xargs kill -9 2>/dev/null || true
lsof -ti:3000 | xargs kill -9 2>/dev/null || true
sleep 1

# Start proxy (web + admin)
log "Starting web server on :5050..."
cd "$SCRIPT_DIR"
python3 proxy.py > /tmp/davai-proxy.log 2>&1 &
PROXY_PID=$!

# Start Node backend
log "Starting app backend on :3000..."
cd "$SCRIPT_DIR/davai-mobile/backend"
node server.js > /tmp/davai-backend.log 2>&1 &
BACKEND_PID=$!

sleep 3

# Health checks
WEB=$(/usr/bin/curl -s -o /dev/null -w "%{http_code}" http://localhost:5050/health 2>/dev/null)
API=$(/usr/bin/curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/ 2>/dev/null || echo "---")

echo ""
ok "Web server  : http://localhost:5050  (HTTP $WEB)"
ok "Admin panel : http://localhost:5050/admin/"
ok "App backend : http://localhost:3000  (HTTP $API)"
echo ""
echo "  Logs:"
echo "    tail -f /tmp/davai-proxy.log"
echo "    tail -f /tmp/davai-backend.log"
echo ""
echo "  Stop:"
echo "    kill $PROXY_PID $BACKEND_PID"
echo "========================================"
echo ""
