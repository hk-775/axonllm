#!/usr/bin/env bash
# AxonLLM Demo Script
# Starts the server, generates real traffic across providers, then opens the dashboard.
# Usage: uv run axon demo

set -euo pipefail
cd "$(dirname "$0")/.."

BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'
PORT=8000
BASE="http://localhost:$PORT"

echo -e "${BOLD}${BLUE}"
echo "  ╔═══════════════════════════════════════╗"
echo "  ║          AxonLLM Demo Setup           ║"
echo "  ║   Multi-Provider LLM Gateway          ║"
echo "  ╚═══════════════════════════════════════╝"
echo -e "${NC}"

# --- 1. Kill any existing server ---
if lsof -ti :$PORT >/dev/null 2>&1; then
  echo -e "${YELLOW}Stopping existing server on port $PORT...${NC}"
  kill $(lsof -ti :$PORT) 2>/dev/null || true
  sleep 1
fi

# --- 2. Start server with demo data ---
echo -e "${CYAN}Starting AxonLLM server...${NC}"
AXON_LOAD_DEMO_DATA=true uv run python serve_dashboard.py > /tmp/axonllm-demo.log 2>&1 &
SERVER_PID=$!
sleep 3

if ! kill -0 $SERVER_PID 2>/dev/null; then
  echo "Server failed to start. Check /tmp/axonllm-demo.log"
  exit 1
fi
echo -e "${GREEN}Server running (PID $SERVER_PID)${NC}"

# --- 3. Generate real traffic across all working providers ---
echo ""
echo -e "${BOLD}Generating real API traffic across providers...${NC}"
echo ""

PROMPTS=(
  "What is the capital of France? Answer in one sentence."
  "Explain recursion in one sentence."
  "What year did the internet become publicly available?"
  "Name three programming languages created after 2010."
  "What is the speed of light in km/s?"
)

MODELS_TO_TEST=(
  "claude-sonnet"
  "grok-3-mini"
  "groq-llama-3.3-70b"
  "groq-llama-3.1-8b"
  "together-llama-3.3-70b"
  "together-deepseek-r1"
  "fireworks-deepseek-v4"
)

USERS=("user-alice" "user-bob" "user-carol" "chat-user" "test-user")

success=0
fail=0

for i in "${!MODELS_TO_TEST[@]}"; do
  model="${MODELS_TO_TEST[$i]}"
  prompt="${PROMPTS[$((i % ${#PROMPTS[@]}))]}"
  user="${USERS[$((i % ${#USERS[@]}))]}"

  printf "  %-28s" "$model"

  result=$(curl -s -m 30 -X POST "$BASE/api/chat" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"$prompt\"}],\"context\":{\"user_id\":\"$user\",\"project_id\":\"proj-alpha\"}}" 2>&1)

  if echo "$result" | python3 -c "import json,sys; d=json.load(sys.stdin); assert 'content' in d" 2>/dev/null; then
    provider=$(echo "$result" | python3 -c "import json,sys; print(json.load(sys.stdin).get('provider','?'))" 2>/dev/null)
    echo -e "${GREEN}✓ OK${NC} (${provider})"
    ((success++))
  else
    error=$(echo "$result" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('error',{}).get('message','unknown')[:50])" 2>/dev/null || echo "timeout/connection error")
    echo -e "${YELLOW}✗ $error${NC}"
    ((fail++))
  fi
done

# --- 4. Run additional requests for routing diversity ---
echo ""
echo -e "${CYAN}Generating routing diversity (multiple requests to multi-provider models)...${NC}"

for run in 1 2 3; do
  for model in "claude-sonnet" "groq-llama-3.3-70b"; do
    user="${USERS[$((RANDOM % ${#USERS[@]}))]}"
    curl -s -m 20 -X POST "$BASE/api/chat" \
      -H "Content-Type: application/json" \
      -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello\"}],\"context\":{\"user_id\":\"$user\",\"project_id\":\"proj-alpha\"}}" >/dev/null 2>&1 &
  done
  wait
done
echo -e "${GREEN}Done.${NC}"

# --- 5. Summary ---
echo ""
echo -e "${BOLD}═══════════════════════════════════════${NC}"
echo -e "${GREEN}  Demo ready!${NC}"
echo -e "${BOLD}═══════════════════════════════════════${NC}"
echo ""
echo -e "  ${BOLD}Dashboard:${NC}  $BASE/admin/dashboard"
echo -e "  ${BOLD}Chat:${NC}       $BASE/chat"
echo -e "  ${BOLD}Playground:${NC} $BASE/playground"
echo -e "  ${BOLD}API:${NC}        $BASE/api/models"
echo ""
echo -e "  Providers tested: ${success} success, ${fail} failed"
echo -e "  Models available: $(curl -s $BASE/api/models | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))' 2>/dev/null || echo '?')"
echo ""
echo -e "  ${YELLOW}Ctrl+C to stop the server${NC}"
echo ""

# --- 6. Open dashboard ---
if command -v open >/dev/null 2>&1; then
  open "$BASE/admin/dashboard"
fi

# Keep running
wait $SERVER_PID
