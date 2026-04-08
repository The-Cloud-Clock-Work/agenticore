#!/usr/bin/env bash
set -euo pipefail

# test_mcp_whitelist.sh — MCP whitelist smoke test
#
# Phase 1: Data validation (instant) — checks disabledMcpServers in ~/.claude.json
# Phase 2: Live call (optional, ~30s) — one ALLOW + one BLOCK call to prove it works
#
# Usage:
#   ./test_mcp_whitelist.sh                    # test anton-agent
#   ./test_mcp_whitelist.sh finops-agent       # test specific agent
#   ./test_mcp_whitelist.sh anton-agent --live # include live API calls
#   NAMESPACE=anton-prod ./test_mcp_whitelist.sh

NAMESPACE="${NAMESPACE:-anton-dev}"
KUBECONFIG="${KUBECONFIG:-/home/iamroot/.kube/config-k3s}"
KUBECTL="${KUBECTL:-/home/iamroot/bin/kubectl}"
TARGET="${1:-anton-agent}"
LIVE="${2:-}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_DIR="${HOME}/.agenticore/logs"
LOG_FILE="${LOG_DIR}/mcp-whitelist-${TIMESTAMP}.json"
mkdir -p "$LOG_DIR"

# --- Expected whitelist per agent ---
declare -A EXPECT_ENABLED
EXPECT_ENABLED[anton-agent]="tools-agent tools-agent-dev tools-notifications tools-notifications-dev"
EXPECT_ENABLED[finops-agent]="tools-agent tools-code tools-observe"
EXPECT_ENABLED[publishing-agent]="tools-agent tools-code tools-pm"
EXPECT_ENABLED[notebooklm-agent]="tools-pm tools-pm-dev"

# --- Expected blocked servers (sample, not exhaustive) ---
declare -A EXPECT_BLOCKED
EXPECT_BLOCKED[anton-agent]="tools-code tools-observe tools-infra tools-aigateway tools-pm"
EXPECT_BLOCKED[finops-agent]="tools-infra tools-notifications tools-pm tools-aigateway"
EXPECT_BLOCKED[publishing-agent]="tools-infra tools-notifications tools-observe tools-aigateway"
EXPECT_BLOCKED[notebooklm-agent]="tools-code tools-observe tools-infra tools-agent tools-notifications"

# --- Live test definitions: "allow_prompt|block_prompt" ---
declare -A LIVE_TESTS
LIVE_TESTS[anton-agent]="Call agentibridge list_agents now. If unavailable say TOOL_NOT_FOUND.|Call github list_issues for The-Cloud-Clock-Work/antoncore. If unavailable say TOOL_NOT_FOUND."
LIVE_TESTS[finops-agent]="Call grafana search_dashboards with query api. If unavailable say TOOL_NOT_FOUND.|Call litellm_tools list_models. If unavailable say TOOL_NOT_FOUND."
LIVE_TESTS[publishing-agent]="Call github get_me. If unavailable say TOOL_NOT_FOUND.|Call grafana list_datasources. If unavailable say TOOL_NOT_FOUND."
LIVE_TESTS[notebooklm-agent]="Check if mediagen tools exist. List them or say TOOL_NOT_FOUND.|Call github list_issues for any repo. If unavailable say TOOL_NOT_FOUND."

# --- Validate ---
if [[ -z "${EXPECT_ENABLED[$TARGET]+x}" ]]; then
    echo "Unknown agent: $TARGET (available: ${!EXPECT_ENABLED[*]})"
    exit 1
fi
if ! KUBECONFIG="$KUBECONFIG" "$KUBECTL" get pod -n "$NAMESPACE" "${TARGET}-0" --no-headers 2>/dev/null | grep -q "Running"; then
    echo "ERROR: ${TARGET}-0 not running in ${NAMESPACE}"
    exit 1
fi

# --- Get actual state from pod ---
POD_DATA=$(KUBECONFIG="$KUBECONFIG" "$KUBECTL" exec -n "$NAMESPACE" "${TARGET}-0" -- python3 -c "
import json, os
from pathlib import Path
c = json.loads(Path.home().joinpath('.claude.json').read_text())
agent = os.environ.get('AGENTIHUB_AGENT', 'unknown')
cfg_path = f'/shared/agentihub/agents/{agent}/package/.agentihooks.json'
cfg = json.load(open(cfg_path)) if os.path.exists(cfg_path) else {}

all_servers = sorted(c.get('mcpServers', {}).keys())
disabled = []
for p, v in c.get('projects', {}).items():
    d = v.get('disabledMcpServers', [])
    if d:
        disabled = d
        break
enabled = sorted(set(all_servers) - set(disabled))

print(json.dumps({
    'agent': agent,
    'profile': cfg.get('profile', ''),
    'config_enabled': cfg.get('enabledMcpServers', []),
    'actual_enabled': enabled,
    'actual_disabled': sorted(disabled),
    'all_servers': all_servers
}))
" 2>/dev/null)

ACTUAL_ENABLED=$(echo "$POD_DATA" | python3 -c "import json,sys; print(' '.join(json.load(sys.stdin)['actual_enabled']))")
ACTUAL_DISABLED=$(echo "$POD_DATA" | python3 -c "import json,sys; print(' '.join(json.load(sys.stdin)['actual_disabled']))")
CONFIG_ENABLED=$(echo "$POD_DATA" | python3 -c "import json,sys; print(' '.join(json.load(sys.stdin)['config_enabled']))")
AGENT_PROFILE=$(echo "$POD_DATA" | python3 -c "import json,sys; print(json.load(sys.stdin)['profile'])")

# --- Header ---
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  MCP Whitelist Smoke Test                                       ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
printf "║  Agent:     %-52s║\n" "${TARGET} (profile: ${AGENT_PROFILE})"
printf "║  Namespace: %-52s║\n" "$NAMESPACE"
printf "║  Config:    %-52s║\n" "$CONFIG_ENABLED"
printf "║  Actual:    %-52s║\n" "$ACTUAL_ENABLED"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

PASS=0
FAIL=0

printf "%-40s %-8s %-8s %-30s\n" "CHECK" "EXPECT" "RESULT" "DETAIL"
printf "%-40s %-8s %-8s %-30s\n" "────────────────────────────────────────" "────────" "────────" "──────────────────────────────"

# --- Phase 1: Data validation ---
set +e  # grep returns 1 on no-match, don't exit

# Check each expected-enabled server is actually enabled
for server in ${EXPECT_ENABLED[$TARGET]}; do
    if echo " $ACTUAL_ENABLED " | grep -q " $server "; then
        printf "%-40s %-8s \033[32m%-8s\033[0m %-30s\n" "$server" "ALLOW" "PASS" "enabled ✓"
        ((PASS++))
    else
        printf "%-40s %-8s \033[31m%-8s\033[0m %-30s\n" "$server" "ALLOW" "FAIL" "NOT in enabled list"
        ((FAIL++))
    fi
done

# Check each expected-blocked server is actually disabled
for server in ${EXPECT_BLOCKED[$TARGET]}; do
    if echo " $ACTUAL_DISABLED " | grep -q " $server "; then
        printf "%-40s %-8s \033[32m%-8s\033[0m %-30s\n" "$server" "BLOCK" "PASS" "disabled ✓"
        ((PASS++))
    else
        printf "%-40s %-8s \033[31m%-8s\033[0m %-30s\n" "$server" "BLOCK" "FAIL" "NOT in disabled list"
        ((FAIL++))
    fi
done

# Check config matches actual
CONFIG_MATCH="true"
for server in ${EXPECT_ENABLED[$TARGET]}; do
    if ! echo " $CONFIG_ENABLED " | grep -q " $server "; then
        CONFIG_MATCH="false"
        break
    fi
done
if [[ "$CONFIG_MATCH" == "true" ]]; then
    printf "%-40s %-8s \033[32m%-8s\033[0m %-30s\n" ".agentihooks.json matches expected" "MATCH" "PASS" "config = expected"
    ((PASS++))
else
    printf "%-40s %-8s \033[31m%-8s\033[0m %-30s\n" ".agentihooks.json matches expected" "MATCH" "FAIL" "config != expected"
    ((FAIL++))
fi

set -e  # re-enable
# --- Phase 2: Live API calls (optional) ---
if [[ "$LIVE" == "--live" ]]; then
    echo ""
    printf "%-40s %-8s %-8s %-30s\n" "── LIVE CALLS ──" "" "" ""

    IFS='|' read -r allow_prompt block_prompt <<< "${LIVE_TESTS[$TARGET]}"

    # ALLOW test
    printf "%-40s %-8s " "Live: ALLOW tool call" "ALLOW"
    payload="$(PROMPT="$allow_prompt" python3 -c "import json,os; print(json.dumps({'model':'agent','messages':[{'role':'user','content':os.environ['PROMPT']}],'max_tokens':200}))")"
    raw=$(echo "$payload" | KUBECONFIG="$KUBECONFIG" timeout 90 "$KUBECTL" exec -i -n "$NAMESPACE" "${TARGET}-0" -- bash -c 'curl -s --max-time 60 -X POST http://localhost:8200/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer ${AGENTICORE_API_KEYS%%,*}" -d @-' 2>/dev/null || echo '{}')
    content=$(echo "$raw" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('choices',[{}])[0].get('message',{}).get('content','EMPTY')[:80])" 2>/dev/null || echo "PARSE_ERROR")
    if echo "$content" | grep -qi "TOOL_NOT_FOUND\|EMPTY\|PARSE_ERROR"; then
        printf "\033[31m%-8s\033[0m %-30s\n" "FAIL" "${content:0:28}"
        ((FAIL++))
    else
        printf "\033[32m%-8s\033[0m %-30s\n" "PASS" "${content:0:28}"
        ((PASS++))
    fi

    # BLOCK test
    printf "%-40s %-8s " "Live: BLOCK tool call" "BLOCK"
    payload="$(PROMPT="$block_prompt" python3 -c "import json,os; print(json.dumps({'model':'agent','messages':[{'role':'user','content':os.environ['PROMPT']}],'max_tokens':200}))")"
    raw=$(echo "$payload" | KUBECONFIG="$KUBECONFIG" timeout 90 "$KUBECTL" exec -i -n "$NAMESPACE" "${TARGET}-0" -- bash -c 'curl -s --max-time 60 -X POST http://localhost:8200/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer ${AGENTICORE_API_KEYS%%,*}" -d @-' 2>/dev/null || echo '{}')
    content=$(echo "$raw" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('choices',[{}])[0].get('message',{}).get('content','EMPTY')[:80])" 2>/dev/null || echo "PARSE_ERROR")
    if echo "$content" | grep -qi "TOOL_NOT_FOUND"; then
        printf "\033[32m%-8s\033[0m %-30s\n" "PASS" "TOOL_NOT_FOUND ✓"
        ((PASS++))
    else
        printf "\033[31m%-8s\033[0m %-30s\n" "FAIL" "${content:0:28}"
        ((FAIL++))
    fi
fi

# --- Summary ---
echo ""
TOTAL=$((PASS + FAIL))
printf "══════════════════════════════════════════════════════════════════\n"
if [[ "$FAIL" -eq 0 ]]; then
    printf "\033[32m  ALL PASS: %d/%d\033[0m\n" "$PASS" "$TOTAL"
else
    printf "\033[31m  %d FAILED, %d passed out of %d\033[0m\n" "$FAIL" "$PASS" "$TOTAL"
fi
echo ""

# --- JSON log ---
python3 << PYEOF
import json
log = {
    "timestamp": "$TIMESTAMP",
    "agent": "$TARGET",
    "namespace": "$NAMESPACE",
    "profile": "$AGENT_PROFILE",
    "config_enabled": "$CONFIG_ENABLED".split(),
    "actual_enabled": "$ACTUAL_ENABLED".split(),
    "pass": $PASS,
    "fail": $FAIL,
    "total": $TOTAL,
    "live": "$LIVE" == "--live"
}
with open("$LOG_FILE", "w") as f:
    json.dump(log, f, indent=2)
print(f"Log: $LOG_FILE")
PYEOF

exit "$FAIL"
