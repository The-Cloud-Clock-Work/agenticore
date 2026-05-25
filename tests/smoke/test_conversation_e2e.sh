#!/usr/bin/env bash
set -euo pipefail

# ===========================================================================
# E2E Conversation Persistence Test
# Tests the full chain: Client → LiteLLM → Agenticore → Claude
# with X-Conversation-Id header forwarding through every hop.
#
# Usage:
#   ./test_conversation_e2e.sh [model] [--direct]
#
#   model:    LiteLLM model name (default: anton-agent)
#   --direct: bypass LiteLLM, hit agent pod directly via port-forward
# ===========================================================================

KCTL="kubectl --kubeconfig /home/iamroot/.kube/config-k3s -n anton-prod"
MODEL="${1:-anton-agent}"
MODE="litellm"
[[ "${2:-}" == "--direct" ]] && MODE="direct"

PASS=0
FAIL=0
TOTAL=0

cleanup() {
    kill "$PF_PID" 2>/dev/null || true
}
trap cleanup EXIT

result() {
    TOTAL=$((TOTAL + 1))
    if [[ "$1" == "PASS" ]]; then
        PASS=$((PASS + 1))
        echo "  ✓ $2"
    else
        FAIL=$((FAIL + 1))
        echo "  ✗ $2"
    fi
}

# ---- Setup port-forward ----
if [[ "$MODE" == "litellm" ]]; then
    echo "Mode: LiteLLM proxy (full chain)"
    $KCTL port-forward svc/litellm 4000:4000 &
    PF_PID=$!
    sleep 2
    BASE="http://localhost:4000/v1"
    KEY=$($KCTL exec litellm-0 -- printenv LITELLM_MASTER_KEY 2>/dev/null)
else
    echo "Mode: Direct to agent pod"
    $KCTL port-forward svc/anton-agent 8200:8200 &
    PF_PID=$!
    sleep 2
    BASE="http://localhost:8200/v1"
    KEY=$($KCTL exec anton-agent-0 -c agenticore -- printenv AGENTICORE_OPENAI_COMPAT_KEY 2>/dev/null)
fi

# ---- Helper ----
call() {
    local conv_id="$1"
    local msg="$2"
    local extra_headers="${3:-}"

    local h_args=(-H "Authorization: Bearer $KEY"
                  -H "Content-Type: application/json"
                  -H "X-Conversation-Id: $conv_id"
                  -H "X-User-Id: e2e-test-user")

    if [[ -n "$extra_headers" ]]; then
        h_args+=(-H "$extra_headers")
    fi

    curl -sS --max-time 120 -X POST "$BASE/chat/completions" \
        "${h_args[@]}" \
        -d "{\"model\":\"$MODEL\",\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"$msg\"}]}" 2>&1
}

extract() {
    python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    if 'error' in d:
        print('ERROR: ' + d['error'].get('message','unknown'))
    else:
        print(d['choices'][0]['message']['content'])
except Exception as e:
    print('PARSE_ERROR: ' + str(e))
" 2>/dev/null
}

# ---- Clear stale sessions ----
$KCTL exec anton-agent-0 -c agenticore -- python3 -c "
import os, redis
r = redis.from_url(os.environ.get('REDIS_URL',''), decode_responses=True)
for k in r.keys('agenticore:session:conv:*'): r.delete(k)
" 2>/dev/null

echo ""
echo "=========================================="
echo " CONVERSATION PERSISTENCE E2E TEST"
echo "=========================================="
echo ""

# ==== TEST BLOCK 1: Multi-turn memory ====
CONV_A=$(uuidgen)
echo "--- Block 1: Multi-turn memory (conv=$CONV_A) ---"

echo "Turn 1: Store 3 facts..."
R=$(call "$CONV_A" "I will tell you 3 facts. Fact A: city=Tokyo. Fact B: year=2030. Fact C: color=red. Reply only: noted." | extract)
echo "  Agent: $R"
[[ "$R" != ERROR* ]] && [[ "$R" != PARSE_ERROR* ]] && result PASS "T1: stored" || result FAIL "T1: $R"

echo "Turn 2: Recall city..."
R=$(call "$CONV_A" "What city? One word." | extract)
echo "  Agent: $R"
echo "$R" | grep -qi "tokyo" && result PASS "T2: Tokyo recalled" || result FAIL "T2: got '$R'"

echo "Turn 3: Recall year..."
R=$(call "$CONV_A" "What year? Just the number." | extract)
echo "  Agent: $R"
echo "$R" | grep -q "2030" && result PASS "T3: 2030 recalled" || result FAIL "T3: got '$R'"

echo "Turn 4: Recall color..."
R=$(call "$CONV_A" "What color? One word." | extract)
echo "  Agent: $R"
echo "$R" | grep -qi "red" && result PASS "T4: red recalled" || result FAIL "T4: got '$R'"

echo "Turn 5: Add a 4th fact..."
R=$(call "$CONV_A" "New fact D: animal=falcon. Reply: noted." | extract)
echo "  Agent: $R"

echo "Turn 6: Recall all 4..."
R=$(call "$CONV_A" "List all 4 facts (city, year, color, animal) as key=value pairs." | extract)
echo "  Agent: $R"
SCORE=0
echo "$R" | grep -qi "tokyo" && SCORE=$((SCORE+1))
echo "$R" | grep -q "2030" && SCORE=$((SCORE+1))
echo "$R" | grep -qi "red" && SCORE=$((SCORE+1))
echo "$R" | grep -qi "falcon" && SCORE=$((SCORE+1))
[[ $SCORE -eq 4 ]] && result PASS "T6: all 4 facts ($SCORE/4)" || result FAIL "T6: only $SCORE/4 facts"

echo ""

# ==== TEST BLOCK 2: Conversation isolation ====
CONV_B=$(uuidgen)
echo "--- Block 2: Isolation (conv=$CONV_B) ---"

echo "Turn 1: Ask about facts from conv A..."
R=$(call "$CONV_B" "What city, year, color, or animal did I mention? If none, say: no prior context." | extract)
echo "  Agent: $R"
echo "$R" | grep -qiE "tokyo|2030|red|falcon" && result FAIL "Isolation: leaked from conv A" || result PASS "Isolation: clean slate"

echo ""

# ==== TEST BLOCK 3: Streaming multi-turn ====
CONV_C=$(uuidgen)
echo "--- Block 3: Streaming path (conv=$CONV_C) ---"

echo "Turn 1: Store via streaming..."
SR=$(curl -sS -N --max-time 60 -X POST "$BASE/chat/completions" \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -H "X-Conversation-Id: $CONV_C" \
    -d "{\"model\":\"$MODEL\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Remember: fruit=mango. Reply: stored.\"}]}" 2>&1)
echo "$SR" | grep -o '"content":"[^"]*"' | tail -2
echo "$SR" | grep -q '"content"' && result PASS "S1: streaming response received" || result FAIL "S1: no content in stream"

echo "Turn 2: Recall via streaming..."
SR2=$(curl -sS -N --max-time 60 -X POST "$BASE/chat/completions" \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -H "X-Conversation-Id: $CONV_C" \
    -d "{\"model\":\"$MODEL\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"What fruit did I say? One word.\"}]}" 2>&1)
echo "$SR2" | grep -o '"content":"[^"]*"' | tail -3
echo "$SR2" | grep -qi "mango" && result PASS "S2: mango recalled via stream" || result FAIL "S2: not recalled"

echo ""

# ==== TEST BLOCK 4: Redis state verification ====
echo "--- Block 4: Redis verification ---"

REDIS_OUT=$($KCTL exec anton-agent-0 -c agenticore -- python3 -c "
import os, redis, json
r = redis.from_url(os.environ.get('REDIS_URL',''), decode_responses=True)
keys = sorted(r.keys('agenticore:session:conv:*'))
for k in keys:
    d = r.hgetall(k)
    status = d.get('status','?')
    sid = d.get('claude_session_id','')[:12]
    stype = d.get('session_type','?')
    print(f'{k[-40:]} status={status} type={stype} sid={sid}...')
print(f'total_sessions={len(keys)}')
" 2>/dev/null)
echo "$REDIS_OUT"

ACTIVE=$(echo "$REDIS_OUT" | grep -c 'status=active' || true)
TOTAL_SESS=$(echo "$REDIS_OUT" | grep -o 'total_sessions=[0-9]*' | cut -d= -f2)
[[ "$ACTIVE" -ge 2 ]] && result PASS "Redis: $ACTIVE active sessions" || result FAIL "Redis: only $ACTIVE active"
[[ -n "$TOTAL_SESS" ]] && [[ "$TOTAL_SESS" -ge 3 ]] && result PASS "Redis: $TOTAL_SESS total sessions created" || result FAIL "Redis: session count unexpected"

echo ""

# ==== TEST BLOCK 5: Tier logging verification ====
echo "--- Block 5: Log verification ---"

LOGS=$($KCTL logs anton-agent-0 -c agenticore --tail=100 2>&1 | grep 'tier=' || true)
TIER_COUNT=$(echo "$LOGS" | grep -c 'tier=header' || true)
echo "  tier=header log entries: $TIER_COUNT"
[[ "$TIER_COUNT" -ge 3 ]] && result PASS "Logs: tier=header entries found ($TIER_COUNT)" || result FAIL "Logs: only $TIER_COUNT tier entries"

echo ""
echo "=========================================="
echo " RESULTS: $PASS passed, $FAIL failed (of $TOTAL)"
echo "=========================================="
[[ $FAIL -eq 0 ]] && echo " ALL PASS" && exit 0
echo " FAILURES DETECTED" && exit 1
