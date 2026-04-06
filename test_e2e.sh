#!/usr/bin/env bash
# End-to-end test against local wrangler dev worker.
# Prerequisites:
#   1. Worker running: cd worker && npm run dev
#
# This uses a temp DB and temp config — your real data is never touched.
# Nothing posts to production HuggingFace — worker points at test-conversations.

set -euo pipefail

# Parse flags
USE_PRESIDIO=false
for arg in "$@"; do
  case "$arg" in
    --presidio) USE_PRESIDIO=true ;;
    *) echo "Usage: $0 [--presidio]"; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
WORKER_URL="http://localhost:8787"
TEST_DB="/tmp/parlance-e2e-test.db"
TEST_CONFIG_DIR="/tmp/parlance-e2e-config"
TEST_CONFIG="$TEST_CONFIG_DIR/config.json"
FIXTURE="$SCRIPT_DIR/tests/fixtures/sample_messages.jsonl"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

step() { echo -e "\n${BOLD}=== $1 ===${NC}"; }
ok()   { echo -e "${GREEN}OK${NC}: $1"; }
fail() { echo -e "${RED}FAIL${NC}: $1"; exit 1; }

# Clean up from previous runs
rm -f "$TEST_DB" "$TEST_DB-wal" "$TEST_DB-shm"
rm -rf "$TEST_CONFIG_DIR"
mkdir -p "$TEST_CONFIG_DIR"

# Check worker is running
step "Checking local worker"
HEALTH=$(curl -sf "$WORKER_URL/health" 2>/dev/null) || fail "Worker not running. Start with: cd worker && npm run dev"
echo "$HEALTH" | "$PYTHON" -m json.tool
REPO=$(echo "$HEALTH" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['repo'])")
if [[ "$REPO" == *"test"* ]]; then
    ok "Worker pointing at test repo: $REPO"
else
    fail "Worker pointing at production repo: $REPO — check worker/.dev.vars HF_REPO"
fi

# Create test config pointing at local worker
step "Setting up test config"
cat > "$TEST_CONFIG" <<EOF
{
  "proxy_url": "$WORKER_URL",
  "consent": true,
  "consent_timestamp": "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)",
  "database": "$TEST_DB",
  "use_presidio": $USE_PRESIDIO,
  "ner_prompted": true,
  "auto_approve": false
}
EOF
ok "Test config at $TEST_CONFIG"

# Override config path for all subsequent commands
export COMMON_PARLANCE_CONFIG="$TEST_CONFIG"

# --- Register (get a test API key from local worker) ---
step "Registration flow"
echo -e "${DIM}Initiating device auth...${NC}"
INIT=$(curl -sf -X POST "$WORKER_URL/register/init")
DEVICE_CODE=$(echo "$INIT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['device_code'])")
USER_CODE=$(echo "$INIT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['user_code'])")
echo "Device code: $DEVICE_CODE"
echo "User code: $USER_CODE"

# Get PoW challenge (uses user code, not device code)
USER_CODE_NOHYPHEN=$(echo "$USER_CODE" | tr -d '-')
echo -e "${DIM}Getting PoW challenge for $USER_CODE_NOHYPHEN...${NC}"
CHALLENGE_RESP=$(curl -sf "$WORKER_URL/register/challenge/$USER_CODE_NOHYPHEN")
CHALLENGE=$(echo "$CHALLENGE_RESP" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['challenge'])")
DIFFICULTY=$(echo "$CHALLENGE_RESP" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['difficulty'])")
echo "Challenge: $CHALLENGE (difficulty: $DIFFICULTY)"

# Solve PoW
echo -e "${DIM}Solving proof-of-work...${NC}"
NONCE=$("$PYTHON" -c "
import hashlib
challenge = '$CHALLENGE'
difficulty = $DIFFICULTY
prefix = '0' * difficulty
nonce = 0
while True:
    h = hashlib.sha256((challenge + str(nonce)).encode()).hexdigest()
    if h.startswith(prefix):
        print(nonce)
        break
    nonce += 1
")
echo "Nonce: $NONCE"

# Complete registration (Turnstile test token always passes)
echo -e "${DIM}Completing registration...${NC}"
COMPLETE=$(curl -sf -X POST "$WORKER_URL/register/complete" \
    -H "Content-Type: application/json" \
    -d "{
        \"user_code\": \"$USER_CODE\",
        \"turnstile_token\": \"test-token\",
        \"pow_nonce\": \"$NONCE\"
    }")
echo "$COMPLETE" | "$PYTHON" -m json.tool

# Poll for API key
echo -e "${DIM}Polling for API key...${NC}"
sleep 1
POLL=$(curl -sf "$WORKER_URL/register/poll/$DEVICE_CODE")
STATUS=$(echo "$POLL" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('status','pending'))")
if [[ "$STATUS" == "complete" ]]; then
    API_KEY=$(echo "$POLL" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['api_key'])")
    ok "Got API key: ${API_KEY:0:16}..."
    # Save to test config
    "$PYTHON" -c "
import json
with open('$TEST_CONFIG') as f: cfg = json.load(f)
cfg['api_key'] = '$API_KEY'
with open('$TEST_CONFIG', 'w') as f: json.dump(cfg, f, indent=2)
"
else
    fail "Registration poll returned: $STATUS"
fi

# --- Import ---
step "Import test conversations"
"$PYTHON" -m common_parlance.cli import "$FIXTURE" -d "$TEST_DB"

# --- Process ---
if $USE_PRESIDIO; then
  step "Process (PII scrubbing — presidio NER)"
  "$PYTHON" -m common_parlance.cli process -d "$TEST_DB"
else
  step "Process (PII scrubbing — regex only)"
  "$PYTHON" -m common_parlance.cli process -d "$TEST_DB" --no-presidio
fi

# --- Review (auto-approve for testing) ---
step "Review (auto-approve)"
"$PYTHON" -m common_parlance.cli review -d "$TEST_DB" --approve-all

# --- Status check before upload ---
step "Pipeline status"
"$PYTHON" -m common_parlance.cli status -d "$TEST_DB"

# --- Upload ---
step "Upload to local worker (-> test HF repo)"
echo -e "${DIM}This will attempt to write to $REPO on HuggingFace.${NC}"
echo -e "${DIM}If the repo doesn't exist yet, upload will fail with a 404 — that's OK for now.${NC}"
"$PYTHON" -m common_parlance.cli upload -d "$TEST_DB" 2>&1 || true

# --- Final status ---
step "Final status"
"$PYTHON" -m common_parlance.cli status -d "$TEST_DB"

step "Done"
echo -e "${GREEN}End-to-end test complete.${NC}"
echo -e "${DIM}Test DB: $TEST_DB${NC}"
echo -e "${DIM}Test config: $TEST_CONFIG${NC}"
echo -e "${DIM}Clean up: rm -f $TEST_DB $TEST_DB-wal $TEST_DB-shm && rm -rf $TEST_CONFIG_DIR${NC}"
