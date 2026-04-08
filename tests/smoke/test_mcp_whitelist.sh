#!/usr/bin/env bash
set -euo pipefail

# test_mcp_whitelist.sh — MCP whitelist smoke test
#
# Tests that agents only see tools from their .agentihooks.json whitelist.
# Two paths: ALLOW (enabled tools work) and BLOCK (disabled tools refused).
#
# Usage:
#   ./test_mcp_whitelist.sh                    # test anton-agent (default)
#   ./test_mcp_whitelist.sh finops-agent       # test specific agent
#   NAMESPACE=anton-prod ./test_mcp_whitelist.sh
#
# Output: table to stdout + JSON log to ~/.agenticore/logs/mcp-whitelist-<ts>.json

NAMESPACE="${NAMESPACE:-anton-dev}"
KUBECONFIG="${KUBECONFIG:-/home/iamroot/.kube/config-k3s}"
KUBECTL="${KUBECTL:-/home/iamroot/bin/kubectl}"
TARGET="${1:-anton-agent}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_DIR="${HOME}/.agenticore/logs"
LOG_FILE="${LOG_DIR}/mcp-whitelist-${TIMESTAMP}.json"

mkdir -p "$LOG_DIR"

# --- Test definitions per agent ---
# Each test: "label|tool_prompt|expected" where expected = ALLOW or BLOCK
declare -A AGENT_TESTS

AGENT_TESTS[anton-agent]="
agentibridge-list|Call agentibridge list_agents now. If unavailable say TOOL_NOT_FOUND.|ALLOW
github-list|Call github list_issues for The-Cloud-Clock-Work/antoncore now. If unavailable say TOOL_NOT_FOUND.|BLOCK
litellm-list|Call litellm_tools list_models now. If unavailable say TOOL_NOT_FOUND.|BLOCK
"

AGENT_TESTS[finops-agent]="
grafana-search|Use the grafana search_dashboards tool with query api. Show result.|ALLOW
github-get_me|Use the github get_me tool. Show result.|ALLOW
agentibridge-list|Use the agentibridge list_agents tool. Show result.|ALLOW
litellm-list_models|Use the litellm_tools list_models tool. If unavailable say TOOL_NOT_FOUND.|BLOCK
notifications-send|Use the notifications send_notification tool. If unavailable say TOOL_NOT_FOUND.|BLOCK
"

AGENT_TESTS[publishing-agent]="
github-get_me|Use the github get_me tool. Show result.|ALLOW
agentibridge-list|Use the agentibridge list_agents tool. Show result.|ALLOW
grafana-search|Use the grafana search_dashboards tool with query api. If unavailable say TOOL_NOT_FOUND.|BLOCK
litellm-list_models|Use the litellm_tools list_models tool. If unavailable say TOOL_NOT_FOUND.|BLOCK
"

AGENT_TESTS[notebooklm-agent]="
mediagen-check|Check if you have any mediagen or paper2slides tools available. List them or say TOOL_NOT_FOUND.|ALLOW
github-list_issues|Use the github list_issues tool for The-Cloud-Clock-Work/antoncore. If unavailable say TOOL_NOT_FOUND.|BLOCK
grafana-search|Use the grafana search_dashboards tool. If unavailable say TOOL_NOT_FOUND.|BLOCK
"

# --- Helpers ---
_call() {
    local pod="$1" prompt="$2"
    # Build JSON payload safely with python env var
    local payload
    payload="$(PROMPT="$prompt" python3 -c "import json,os; print(json.dumps({'model':'agent','messages':[{'role':'user','content':os.environ['PROMPT']}],'max_tokens':300}))")"

    echo "$payload" | KUBECONFIG="$KUBECONFIG" timeout 120 "$KUBECTL" exec -i -n "$NAMESPACE" "${pod}-0" -- \
        bash -c 'API_KEY="${AGENTICORE_API_KEYS%%,*}"; curl -s --max-time 90 -X POST http://localhost:8200/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer $API_KEY" -d @-' 2>/dev/null
}

_extract() {
    python3 -c "
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    c = d.get('choices', [{}])[0].get('message', {}).get('content', '')
    if not c:
        c = d.get('error', {}).get('message', 'NO_RESPONSE')
    print(c[:200].replace('\n', ' '))
except:
    print('PARSE_ERROR: ' + raw[:100])
"
}

_judge() {
    local response="$1" expected="$2"
    local upper_resp
    upper_resp="$(echo "$response" | tr '[:lower:]' '[:upper:]')"

    if [[ "$expected" == "ALLOW" ]]; then
        if echo "$upper_resp" | grep -q "TOOL_NOT_FOUND"; then
            echo "FAIL"
        elif echo "$upper_resp" | grep -q "PARSE_ERROR\|NO_RESPONSE"; then
            echo "FAIL"
        else
            echo "PASS"
        fi
    else
        if echo "$upper_resp" | grep -q "TOOL_NOT_FOUND"; then
            echo "PASS"
        else
            echo "FAIL"
        fi
    fi
}

# --- Validate target ---
if [[ -z "${AGENT_TESTS[$TARGET]+x}" ]]; then
    echo "Unknown agent: $TARGET"
    echo "Available: ${!AGENT_TESTS[*]}"
    exit 1
fi

# Check pod running
if ! KUBECONFIG="$KUBECONFIG" "$KUBECTL" get pod -n "$NAMESPACE" "${TARGET}-0" --no-headers 2>/dev/null | grep -q "Running"; then
    echo "ERROR: ${TARGET}-0 not running in ${NAMESPACE}"
    exit 1
fi

# --- Get agent whitelist ---
WHITELIST=$(KUBECONFIG="$KUBECONFIG" "$KUBECTL" exec -n "$NAMESPACE" "${TARGET}-0" -- python3 -c "
import json
from pathlib import Path
import os
agent = os.environ.get('AGENTIHUB_AGENT', 'unknown')
cfg = Path(f'/shared/agentihub/agents/{agent}/package/.agentihooks.json')
if cfg.exists():
    print(json.dumps(json.load(open(cfg)).get('enabledMcpServers', [])))
else:
    print('[]')
" 2>/dev/null | grep -v Defaulted)

# --- Header ---
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  MCP Whitelist Smoke Test                                       ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
printf "║  Agent:     %-52s║\n" "$TARGET"
printf "║  Namespace: %-52s║\n" "$NAMESPACE"
printf "║  Whitelist: %-52s║\n" "$WHITELIST"
printf "║  Time:      %-52s║\n" "$TIMESTAMP"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
printf "%-30s %-8s %-8s %-60s\n" "TOOL" "EXPECT" "RESULT" "RESPONSE"
printf "%-30s %-8s %-8s %-60s\n" "------------------------------" "--------" "--------" "------------------------------------------------------------"

PASS=0
FAIL=0
JSON_RESULTS="[]"

while IFS='|' read -r label prompt expected; do
    [[ -z "$label" ]] && continue

    printf "%-30s %-8s " "$label" "$expected"

    raw=$(_call "$TARGET" "$prompt" 2>&1)
    response=$(echo "$raw" | _extract)
    verdict=$(_judge "$response" "$expected")

    if [[ "$verdict" == "PASS" ]]; then
        printf "\033[32m%-8s\033[0m " "$verdict"
        ((PASS++))
    else
        printf "\033[31m%-8s\033[0m " "$verdict"
        ((FAIL++))
    fi

    # Truncate response for display
    display_resp="${response:0:58}"
    printf "%-60s\n" "$display_resp"

    # Accumulate JSON
    JSON_RESULTS=$(PREV="$JSON_RESULTS" TOOL="$label" EXP="$expected" VERD="$verdict" RESP="$response" python3 -c "
import json, os
results = json.loads(os.environ['PREV'])
results.append({
    'tool': os.environ['TOOL'],
    'expected': os.environ['EXP'],
    'verdict': os.environ['VERD'],
    'response': os.environ['RESP'][:300]
})
print(json.dumps(results))
" 2>/dev/null || echo "$JSON_RESULTS")

done <<< "${AGENT_TESTS[$TARGET]}"

echo ""
printf "══════════════════════════════════════════════════════════════════\n"
TOTAL=$((PASS + FAIL))
if [[ "$FAIL" -eq 0 ]]; then
    printf "\033[32m  ALL PASS: %d/%d tests passed\033[0m\n" "$PASS" "$TOTAL"
else
    printf "\033[31m  %d FAILED, %d passed out of %d\033[0m\n" "$FAIL" "$PASS" "$TOTAL"
fi
echo ""

# --- Write JSON log ---
python3 -c "
import json
log = {
    'timestamp': '$TIMESTAMP',
    'agent': '$TARGET',
    'namespace': '$NAMESPACE',
    'whitelist': json.loads('$WHITELIST'),
    'pass': $PASS,
    'fail': $FAIL,
    'total': $TOTAL,
    'tests': json.loads('$JSON_RESULTS')
}
with open('$LOG_FILE', 'w') as f:
    json.dump(log, f, indent=2)
print(f'Log: $LOG_FILE')
" 2>/dev/null || echo "Warning: failed to write log"

exit "$FAIL"
