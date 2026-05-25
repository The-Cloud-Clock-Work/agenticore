#!/usr/bin/env bash
# Deterministic end-to-end audit of the SSE streaming pipeline.
#
# Drives a real conversation with a live agent pod, observes every layer the
# event passes through (client SSE, Redis stream, pod logs, claude transcript),
# and writes timestamped artifacts to /tmp/sse-audit/<run-id>/ for later review.
#
# Invocation:
#   ./tests/smoke/verify_streaming_pipeline.sh [agent]
#   ./tests/smoke/verify_streaming_pipeline.sh [agent] --replay <run-id>
#
# Exit codes: 0 = PASS, 1 = FAIL, 2 = preflight/setup error.
set -euo pipefail

# ─── Constants ──────────────────────────────────────────────────────────────
KCTL="kubectl --kubeconfig ${KUBECONFIG:-/home/iamroot/.kube/config-k3s}"
NS="${NS:-anton-prod}"
AGENT="${1:-streaming-test}"
PORT="${PORT:-8200}"
AUDIT_ROOT="${AUDIT_ROOT:-/tmp/sse-audit}"
TIMEOUT_CURL="${TIMEOUT_CURL:-180}"
TRANSCRIPT_WAIT="${TRANSCRIPT_WAIT:-3}"

# ─── Replay mode ────────────────────────────────────────────────────────────
if [[ "${2:-}" == "--replay" && -n "${3:-}" ]]; then
    RUN_DIR="$AUDIT_ROOT/$3"
    if [[ ! -f "$RUN_DIR/audit-report.md" ]]; then
        echo "FATAL: no audit-report.md in $RUN_DIR" >&2
        exit 2
    fi
    cat "$RUN_DIR/audit-report.md"
    report_json="$RUN_DIR/audit-report.json"
    if [[ -f "$report_json" ]]; then
        if python3 -c "import json; exit(0 if json.load(open('$report_json')).get('overall_pass') else 1)"; then
            exit 0
        else
            exit 1
        fi
    fi
    exit 0
fi

# ─── Setup ──────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS="$SCRIPT_DIR/verify_pipeline_helpers.py"
if [[ ! -f "$HELPERS" ]]; then
    echo "FATAL: helpers missing at $HELPERS" >&2
    exit 2
fi

RUN_ID="$(date -u +%Y%m%d-%H%M%S)-$(head -c 6 /dev/urandom | base64 | tr -dc 'a-z0-9' | head -c 6)"
RUN_DIR="$AUDIT_ROOT/$RUN_ID"
mkdir -p "$RUN_DIR"
PREFLIGHT_LOG="$RUN_DIR/preflight.log"
POD_STATE_JSON="$RUN_DIR/pod-state.json"
SSE_RAW="$RUN_DIR/sse-stream.raw"
SSE_CHUNKS="$RUN_DIR/sse-chunks.jsonl"
REDIS_EVENTS="$RUN_DIR/redis-events.jsonl"
POD_LOGS="$RUN_DIR/pod-logs.log"
TRANSCRIPT="$RUN_DIR/transcript.jsonl"
TRANSCRIPT_BLOCKS="$RUN_DIR/transcript-blocks.jsonl"
META="$RUN_DIR/run-meta.json"
REPORT_JSON="$RUN_DIR/audit-report.json"
REPORT_MD="$RUN_DIR/audit-report.md"

echo "═══ SSE Pipeline Audit ═══"
echo "run_id:  $RUN_ID"
echo "agent:   $AGENT"
echo "ns:      $NS"
echo "dir:     $RUN_DIR"

# Background PIDs we need to clean up
OBSERVER_REDIS_PID=""
OBSERVER_LOGS_PID=""
PF_PID=""
cleanup() {
    local rc=$?
    [[ -n "$OBSERVER_REDIS_PID" ]] && kill "$OBSERVER_REDIS_PID" 2>/dev/null || true
    [[ -n "$OBSERVER_LOGS_PID" ]] && kill "$OBSERVER_LOGS_PID" 2>/dev/null || true
    [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true
    exit $rc
}
trap cleanup EXIT INT TERM

log_pf() { echo "$@" | tee -a "$PREFLIGHT_LOG"; }

# ─── Phase 1: Preflight ─────────────────────────────────────────────────────
echo ""
echo "═══ Phase 1: Preflight ═══"

POD="${AGENT}-0"
pod_phase=$($KCTL -n "$NS" get pod "$POD" -o jsonpath='{.status.phase}' 2>/dev/null || echo "missing")
if [[ "$pod_phase" != "Running" ]]; then
    echo "FATAL: pod $POD not Running (phase=$pod_phase)" >&2
    exit 2
fi
log_pf "pod.phase = $pod_phase"

pod_image_id=$($KCTL -n "$NS" get pod "$POD" -o jsonpath='{.status.containerStatuses[?(@.name=="agenticore")].imageID}' 2>/dev/null || echo "")
log_pf "pod.imageID = $pod_image_id"

ghcr_dev_sha=""
if command -v gh >/dev/null 2>&1; then
    ghcr_dev_sha=$(gh api "/orgs/The-Cloud-Clockwork/packages/container/agenticore/versions?per_page=20" \
        --jq '[.[] | select(.metadata.container.tags[]? == "dev")] | .[0].name' 2>/dev/null || echo "")
fi
log_pf "ghcr.:dev = $ghcr_dev_sha"

image_match="unknown"
if [[ -n "$ghcr_dev_sha" && -n "$pod_image_id" ]]; then
    if [[ "$pod_image_id" == *"$ghcr_dev_sha"* ]]; then
        image_match="true"
    else
        image_match="false-warn"
    fi
fi
log_pf "image.match = $image_match"

# agentihooks is a PyPI dependency — resolve the relay script via Python
# import so this works under PyPI / PATH / URL install modes equally.
# Note: the PyPI dist ships `hooks/` (not `agentihooks/`) as the top-level module.
event_relay_path=$($KCTL -n "$NS" exec "$POD" -c agenticore -- /opt/venv/bin/python -c \
    'import hooks.observability.event_relay as m; print(m.__file__)' 2>/dev/null | tr -d '\r')
if [[ -z "$event_relay_path" ]]; then
    echo "FATAL: hooks.observability.event_relay not importable in pod $POD" >&2
    log_pf "event_relay = MISSING"
    exit 2
fi
relay_sha=$($KCTL -n "$NS" exec "$POD" -c agenticore -- sha256sum "$event_relay_path" 2>/dev/null | awk '{print $1}' || echo "")
if [[ -z "$relay_sha" ]]; then
    echo "FATAL: $event_relay_path missing in pod $POD" >&2
    log_pf "event_relay = MISSING"
    exit 2
fi
log_pf "event_relay.path = $event_relay_path"
log_pf "event_relay.sha256 = $relay_sha"

settings_json=$($KCTL -n "$NS" exec "$POD" -c agenticore -- cat /shared/.claude/settings.json 2>/dev/null || echo "{}")
if ! echo "$settings_json" | grep -q "event_relay.py"; then
    echo "FATAL: settings.json does not wire event_relay.py" >&2
    log_pf "settings.json = NO event_relay wiring"
    exit 2
fi
for hook_type in PostToolUse Stop Notification; do
    if ! python3 -c "
import json, sys
d = json.loads(sys.argv[1])
hooks = d.get('hooks', {}).get('$hook_type', [])
for entry in hooks:
    for h in entry.get('hooks', []):
        if 'event_relay.py' in h.get('command', ''):
            sys.exit(0)
sys.exit(1)
" "$settings_json" 2>/dev/null; then
        echo "FATAL: $hook_type not wired to event_relay.py" >&2
        exit 2
    fi
done
log_pf "settings.json = event_relay wired on PostToolUse + Stop + Notification"

if ! $KCTL -n "$NS" exec "$POD" -c agenticore -- python3 -c \
    "import os,redis; redis.from_url(os.environ['REDIS_URL']).ping()" >/dev/null 2>&1; then
    echo "FATAL: Redis ping failed from pod" >&2
    exit 2
fi
log_pf "redis.ping = OK"

agent_id_env=$($KCTL -n "$NS" exec "$POD" -c agenticore -- bash -c 'echo $AGENTIHUB_AGENT' 2>/dev/null | tr -d '\r\n')
key_prefix=$($KCTL -n "$NS" exec "$POD" -c agenticore -- bash -c 'echo ${REDIS_KEY_PREFIX:-agenticore}' 2>/dev/null | tr -d '\r\n')
log_pf "AGENTIHUB_AGENT = $agent_id_env"
log_pf "REDIS_KEY_PREFIX = $key_prefix"

python3 -c "
import json
json.dump({
    'pod': '$POD',
    'pod_phase': '$pod_phase',
    'pod_image_id': '$pod_image_id',
    'ghcr_dev_sha': '$ghcr_dev_sha',
    'image_match': '$image_match',
    'event_relay_sha256': '$relay_sha',
    'agentihub_agent': '$agent_id_env',
    'redis_key_prefix': '$key_prefix',
    'preflight_pass': True,
}, open('$POD_STATE_JSON', 'w'), indent=2)
"
echo "preflight: OK"

# ─── Phase 2: Correlation setup ─────────────────────────────────────────────
echo ""
echo "═══ Phase 2: Correlation setup ═══"

CORRELATION_UUID=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)
RUN_SHORT=$(echo "$RUN_ID" | tr -dc 'a-z0-9' | head -c 12)
MARK_RAND=$(head -c 8 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 8)
MARKER="SSE-AUDIT-${RUN_SHORT}-${MARK_RAND}"
STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

echo "correlation_uuid: $CORRELATION_UUID"
echo "marker:           $MARKER"

$KCTL -n "$NS" exec "$POD" -c agenticore -- python3 -c "
import os, redis
r = redis.from_url(os.environ['REDIS_URL'])
r.delete('${key_prefix}:stream_config:${agent_id_env}')
" >/dev/null 2>&1 || true

python3 -c "
import json
json.dump({
    'run_id': '$RUN_ID',
    'agent': '$AGENT',
    'pod': '$POD',
    'correlation_uuid': '$CORRELATION_UUID',
    'marker': '$MARKER',
    'started_at': '$STARTED_AT',
    'artifact_dir': '$RUN_DIR',
    'redis_key_prefix': '$key_prefix',
}, open('$META', 'w'), indent=2)
"

# ─── Phase 3: Start parallel observers ──────────────────────────────────────
echo ""
echo "═══ Phase 3: Start observers ═══"

$KCTL -n "$NS" exec -i "$POD" -c agenticore -- python3 -u -c "
import json, os, sys, time, redis
r = redis.from_url(os.environ['REDIS_URL'], decode_responses=True)
key = '${key_prefix}:events:${CORRELATION_UUID}'
last = '0-0'
deadline = time.time() + 300
while time.time() < deadline:
    try:
        result = r.xread({key: last}, count=100, block=500)
    except Exception:
        time.sleep(0.5); continue
    if not result:
        continue
    for _stream, entries in result:
        for eid, fields in entries:
            last = eid
            print(json.dumps({'entry_id': eid, 'fields': fields}), flush=True)
            if fields.get('event_type') == 'done':
                sys.exit(0)
" > "$REDIS_EVENTS" 2>/dev/null &
OBSERVER_REDIS_PID=$!
echo "observer.redis PID = $OBSERVER_REDIS_PID"

($KCTL -n "$NS" logs -f "$POD" -c agenticore --since=5s 2>&1 \
    | grep --line-buffered -E "${CORRELATION_UUID}|${MARKER}" \
    > "$POD_LOGS") &
OBSERVER_LOGS_PID=$!
echo "observer.logs  PID = $OBSERVER_LOGS_PID"

# ─── Phase 4: Conversation ──────────────────────────────────────────────────
echo ""
echo "═══ Phase 4: Conversation ═══"

$KCTL -n "$NS" port-forward "svc/$AGENT" "$PORT:8200" >/tmp/sse-audit-pf.log 2>&1 &
PF_PID=$!
sleep 2

echo "→ priming /show-all"
curl -sS -N --max-time 30 -X POST "http://localhost:$PORT/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"/show-all"}]}' \
    >/dev/null 2>&1 || true

PROMPT="Run exactly this bash command: echo MARKER=${MARKER}. Then in one sentence, tell me what the command printed."
BODY=$(python3 -c "
import json, sys
print(json.dumps({
    'model': 'sonnet',
    'stream': True,
    'messages': [{'role': 'user', 'content': sys.argv[1]}],
}))
" "$PROMPT")

echo "→ main turn (capturing SSE to $SSE_RAW)"
curl -sS -N --max-time "$TIMEOUT_CURL" -X POST "http://localhost:$PORT/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "X-Request-ID: $CORRELATION_UUID" \
    -d "$BODY" > "$SSE_RAW" 2>&1 || true

chunk_count=$(grep -c '^data:' "$SSE_RAW" || echo 0)
echo "SSE chunks received: $chunk_count"

# Parse SSE chunks to jsonl
python3 - <<PYEOF
import json, sys
sys.path.insert(0, "$SCRIPT_DIR")
from verify_pipeline_helpers import parse_sse_stream
with open("$SSE_RAW") as f:
    text = f.read()
chunks = parse_sse_stream(text)
with open("$SSE_CHUNKS", "w") as f:
    for i, c in enumerate(chunks):
        f.write(json.dumps({"seq": i, **{k: v for k, v in c.items() if k != "raw"}}) + "\n")
print(f"parsed chunks: {len(chunks)}")
PYEOF

# Give observers a moment to drain
sleep 2
kill "$OBSERVER_REDIS_PID" 2>/dev/null || true
OBSERVER_REDIS_PID=""
kill "$OBSERVER_LOGS_PID" 2>/dev/null || true
OBSERVER_LOGS_PID=""
kill "$PF_PID" 2>/dev/null || true
PF_PID=""

redis_count=$(wc -l < "$REDIS_EVENTS" 2>/dev/null | tr -d ' ' || echo 0)
echo "Redis events captured: $redis_count"

# ─── Phase 5: Transcript capture ────────────────────────────────────────────
echo ""
echo "═══ Phase 5: Transcript capture ═══"

sleep "$TRANSCRIPT_WAIT"

latest_transcript=$($KCTL -n "$NS" exec "$POD" -c agenticore -- bash -c \
    'ls -t /shared/.claude/projects/*/*.jsonl 2>/dev/null | head -1' 2>/dev/null | tr -d '\r' || echo "")
if [[ -z "$latest_transcript" ]]; then
    echo "WARN: no transcript file found"
    : > "$TRANSCRIPT"
else
    echo "transcript: $latest_transcript"
    $KCTL -n "$NS" exec "$POD" -c agenticore -- cat "$latest_transcript" > "$TRANSCRIPT" 2>/dev/null || true
fi

python3 - <<PYEOF
import json, sys
sys.path.insert(0, "$SCRIPT_DIR")
from verify_pipeline_helpers import parse_transcript_blocks
try:
    text = open("$TRANSCRIPT").read()
except FileNotFoundError:
    text = ""
blocks = parse_transcript_blocks(text)
with open("$TRANSCRIPT_BLOCKS", "w") as f:
    for b in blocks:
        f.write(json.dumps(b) + "\n")
print(f"transcript blocks: {len(blocks)}")
PYEOF

# ─── Phase 6: Cross-validation ──────────────────────────────────────────────
echo ""
echo "═══ Phase 6: Cross-validation ═══"

ENDED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
python3 -c "
import json
m = json.load(open('$META'))
m['ended_at'] = '$ENDED_AT'
json.dump(m, open('$META', 'w'), indent=2)
"

python3 "$HELPERS" "$SSE_RAW" "$REDIS_EVENTS" "$TRANSCRIPT" "$META" "$REPORT_JSON" "$REPORT_MD"
RC=$?

# ─── Phase 7: Report ────────────────────────────────────────────────────────
echo ""
echo "═══ Phase 7: Report ═══"
cat "$REPORT_MD"
echo ""
echo "artifacts: $RUN_DIR"

exit $RC
