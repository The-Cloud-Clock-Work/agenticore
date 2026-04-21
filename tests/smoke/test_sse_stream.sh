#!/usr/bin/env bash
# SSE streaming smoke test for agenticore real-time event delivery.
#
# Usage:
#   ./tests/smoke/test_sse_stream.sh [agent-name]
#
# Default agent: brain-keeper. Port-forwards the agent pod's :8200 to
# localhost, then runs an 8-case matrix exercising the pseudo-slash toggle
# system, sticky persistence, and the non-streaming fallback.
#
# Requires:
#   - kubectl with KUBECONFIG pointing at the dev cluster
#   - jq (for parsing config keys)
#   - Pod must be running with the new agenticore image AND
#     /shared/agentihooks/hooks/observability/event_relay.py present
set -euo pipefail

KCTL="kubectl --kubeconfig ${KUBECONFIG:-/home/iamroot/.kube/config-k3s}"
NS="${NS:-anton-dev}"
AGENT="${1:-brain-keeper}"
PORT="${PORT:-8200}"

echo "=== SSE Stream Smoke Test ==="
echo "Agent:     $AGENT"
echo "Namespace: $NS"
echo "Port:      $PORT"

KEY=$($KCTL get secret agenticore-secrets -n "$NS" -o jsonpath='{.data.AGENTICORE_OPENAI_COMPAT_KEY}' 2>/dev/null | base64 -d || true)

$KCTL port-forward -n "$NS" "svc/$AGENT" "$PORT:8200" >/tmp/sse-pf.log 2>&1 &
PF_PID=$!
trap 'kill $PF_PID 2>/dev/null || true' EXIT
sleep 3

auth_args=()
if [[ -n "$KEY" ]]; then
  auth_args=(-H "Authorization: Bearer $KEY")
fi

call() {
  local prompt="$1"
  curl -sS -N --max-time 120 -X POST "http://localhost:$PORT/v1/chat/completions" \
    "${auth_args[@]}" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg p "$prompt" '{model:"sonnet",stream:true,messages:[{role:"user",content:$p}]}')"
}

call_no_stream() {
  curl -sS --max-time 180 -X POST "http://localhost:$PORT/v1/chat/completions" \
    "${auth_args[@]}" \
    -H "Content-Type: application/json" \
    -d '{"model":"sonnet","stream":false,"messages":[{"role":"user","content":"say one word: ok"}]}'
}

PASS=0
FAIL=0
declare -a FAILED_TESTS

assert() {
  local label="$1"; local cond="$2"
  if eval "$cond"; then
    echo "  PASS: $label"
    PASS=$((PASS+1))
  else
    echo "  FAIL: $label"
    FAIL=$((FAIL+1))
    FAILED_TESTS+=("$label")
  fi
}

# Reset sticky state to default
echo
echo "--- preflight: reset to default visibility ---"
call "/hide-all" >/tmp/sse-reset.sse
echo "  reset done"

echo
echo "--- TEST 1: default visibility (text only) ---"
call "say the word OK and stop" >/tmp/t1.sse
assert "saw [DONE]" "grep -q '\\[DONE\\]' /tmp/t1.sse"
assert "no thinking" "! grep -q 'x_agenticore_event_type.*thinking' /tmp/t1.sse"
assert "no tool_use" "! grep -q 'x_agenticore_event_type.*tool_use' /tmp/t1.sse"

echo
echo "--- TEST 2: /show-thinking enables thinking deltas ---"
call "Plan a 3-step approach to refactor a Python function. Think carefully. /show-thinking" >/tmp/t2.sse
assert "saw [DONE]" "grep -q '\\[DONE\\]' /tmp/t2.sse"
assert "thinking delta present" "grep -q 'x_agenticore_event_type.*thinking' /tmp/t2.sse"

echo
echo "--- TEST 3: sticky thinking persists in stream_config ---"
call "/stream-status" >/tmp/t3.sse
assert "stream_config emitted" "grep -q 'stream_config' /tmp/t3.sse"
assert "show_thinking is true" 'grep -q "show_thinking..: true" /tmp/t3.sse'

echo
echo "--- TEST 4: /hide-thinking flips it off ---"
call "say bye /hide-thinking" >/tmp/t4.sse
assert "saw [DONE]" "grep -q '\\[DONE\\]' /tmp/t4.sse"
call "What is 1 plus 1? Think it out." >/tmp/t4b.sse
assert "thinking hidden" "! grep -q 'x_agenticore_event_type.*thinking' /tmp/t4b.sse"

echo
echo "--- TEST 5: /show-tools enables tool deltas ---"
call "Run the bash command: echo tool-test-output. Then say done. /show-tools" >/tmp/t5.sse
assert "saw [DONE]" "grep -q '\\[DONE\\]' /tmp/t5.sse"
assert "tool_use delta present" "grep -q 'x_agenticore_event_type.*tool_use' /tmp/t5.sse"

echo
echo "--- TEST 6: /stream-status returns config snapshot ---"
call "/stream-status" >/tmp/t6.sse
assert "stream_config event present" "grep -q 'stream_config' /tmp/t6.sse"

echo
echo "--- TEST 7: /hide-all resets all flags ---"
call "/hide-all" >/tmp/t7.sse
call "say one word" >/tmp/t7b.sse
assert "no thinking after hide-all" "! grep -q 'x_agenticore_event_type.*thinking' /tmp/t7b.sse"
assert "no tool_use after hide-all" "! grep -q 'x_agenticore_event_type.*tool_use' /tmp/t7b.sse"

echo
echo "--- TEST 8: non-streaming stream=false still works ---"
call_no_stream >/tmp/t8.json
assert "non-stream returns chat.completion" "grep -q 'chat.completion' /tmp/t8.json"

echo
echo "=== RESULTS: $PASS passed, $FAIL failed ==="
if [[ $FAIL -gt 0 ]]; then
  echo "Failed:"
  for t in "${FAILED_TESTS[@]}"; do echo "  - $t"; done
  exit 1
fi
echo "All tests passed."
