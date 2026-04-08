#!/usr/bin/env bash
set -euo pipefail

# test_mcp_whitelist.sh — Smoke test for MCP whitelist rendering
#
# Tests that the pre-call agentihooks init --repo renders disabledMcpServers
# correctly, so agents only see tools from their .agentihooks.json whitelist.
#
# Usage:
#   ./test_mcp_whitelist.sh                          # test all agents
#   ./test_mcp_whitelist.sh anton-agent               # test specific agent
#   NAMESPACE=anton-prod ./test_mcp_whitelist.sh      # target prod
#
# Prerequisites:
#   - KUBECONFIG set for K3s cluster
#   - Agent pods running with agenticore image that has render_mcp_whitelist
#   - .agentihooks.json committed in agentihub for each agent

NAMESPACE="${NAMESPACE:-anton-dev}"
KUBECONFIG="${KUBECONFIG:-/home/iamroot/.kube/config-k3s}"
KUBECTL="${KUBECTL:-/home/iamroot/bin/kubectl}"
TARGET_AGENT="${1:-all}"

PASS=0
FAIL=0
SKIP=0

# --- Agent definitions: agent_name → enabled_server → tool_to_test ---
# Format: "agent_pod|enabled_server|tool_prompt|disabled_server|disabled_tool_prompt"
TESTS=(
  "anton-agent|tools-agent|Use the agentibridge list_agents tool to list registered agents. If not available say TOOL_NOT_FOUND.|tools-code|Use the github list_issues tool to list issues in The-Cloud-Clock-Work/antoncore. If not available say TOOL_NOT_FOUND."
  "finops-agent|tools-observe|Use the grafana search_dashboards tool to search for dashboards with query 'api'. If not available say TOOL_NOT_FOUND.|tools-pm|Use the mediagen tool to generate an image. If not available say TOOL_NOT_FOUND."
  "publishing-agent|tools-code|Use the github get_me tool to show the authenticated user. If not available say TOOL_NOT_FOUND.|tools-observe|Use the grafana list_datasources tool. If not available say TOOL_NOT_FOUND."
)

_call_agent() {
    local pod="$1"
    local prompt="$2"
    KUBECONFIG="$KUBECONFIG" "$KUBECTL" exec -n "$NAMESPACE" "${pod}-0" -- bash -c "
        API_KEY=\"\${AGENTICORE_API_KEYS%%,*}\"
        curl -s -X POST http://localhost:8200/v1/chat/completions \
          -H 'Content-Type: application/json' \
          -H \"Authorization: Bearer \$API_KEY\" \
          -d '{
            \"model\": \"agent\",
            \"messages\": [{\"role\": \"user\", \"content\": \"${prompt}\"}],
            \"max_tokens\": 400
          }' 2>&1
    " 2>&1
}

_check_result() {
    local raw="$1"
    local expect_found="$2"  # "yes" or "no"

    local content
    content=$(echo "$raw" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    choices = d.get('choices', [])
    if choices:
        print(choices[0].get('message', {}).get('content', ''))
    else:
        err = d.get('error', {}).get('message', '')
        print(f'ERROR: {err}')
except:
    print('PARSE_ERROR')
" 2>/dev/null)

    if [[ "$expect_found" == "yes" ]]; then
        if echo "$content" | grep -qi "TOOL_NOT_FOUND"; then
            echo "FAIL (expected tool to work, got TOOL_NOT_FOUND)"
            return 1
        elif echo "$content" | grep -qi "ERROR"; then
            echo "FAIL (error: ${content:0:100})"
            return 1
        else
            echo "PASS (tool executed)"
            return 0
        fi
    else
        if echo "$content" | grep -qi "TOOL_NOT_FOUND"; then
            echo "PASS (tool correctly blocked)"
            return 0
        else
            echo "FAIL (expected TOOL_NOT_FOUND, got: ${content:0:100})"
            return 1
        fi
    fi
}

echo "=== MCP Whitelist Smoke Test ==="
echo "Namespace: ${NAMESPACE}"
echo ""

for test_def in "${TESTS[@]}"; do
    IFS='|' read -r pod enabled_server enabled_prompt disabled_server disabled_prompt <<< "$test_def"

    if [[ "$TARGET_AGENT" != "all" && "$TARGET_AGENT" != "$pod" ]]; then
        continue
    fi

    # Check pod is running
    if ! KUBECONFIG="$KUBECONFIG" "$KUBECTL" get pod -n "$NAMESPACE" "${pod}-0" --no-headers 2>/dev/null | grep -q "Running"; then
        echo "[$pod] SKIP — pod not running"
        ((SKIP++))
        continue
    fi

    echo "[$pod] Testing enabled server: ${enabled_server}"
    raw=$(_call_agent "$pod" "$enabled_prompt")
    result=$(_check_result "$raw" "yes")
    echo "  → $result"
    if [[ "$result" == PASS* ]]; then ((PASS++)); else ((FAIL++)); fi

    echo "[$pod] Testing disabled server: ${disabled_server}"
    raw=$(_call_agent "$pod" "$disabled_prompt")
    result=$(_check_result "$raw" "no")
    echo "  → $result"
    if [[ "$result" == PASS* ]]; then ((PASS++)); else ((FAIL++)); fi

    echo ""
done

echo "=== Results: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped ==="
exit "$FAIL"
