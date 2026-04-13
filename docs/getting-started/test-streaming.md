---
title: Test Streaming
nav_order: 4
---

# Test SSE Streaming Yourself

Five-minute walkthrough. Port-forward an agent pod, send a request, watch thinking and tool calls stream live.

## Prerequisites

- A deployed agent pod running `ghcr.io/the-cloud-clock-work/agenticore:dev` or later
- `kubectl` access to the namespace (default examples use `anton-dev`)
- `curl`, `jq`, `python3` locally

If your pod runs an older image, see the [rollout section](#propagating-to-all-agents) below.

## 1. Port-forward the agent

Pick any agent pod. The examples use `streaming-test` (a disposable pod for experiments), but you can use `docgen-agent`, `anton-agent`, or any other agent running the `:dev` image.

```bash
kubectl port-forward -n anton-dev svc/streaming-test 8200:8200 &
```

## 2. First call — enable visibility

By default, only assistant text is streamed. Enable thinking and tool events once (sticky per agent, persists in Redis):

```bash
curl -sN http://localhost:8200/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"/show-all"}]}'
```

Response is instant, just the visibility config being persisted.

## 3. Have a conversation — watch events arrive live

```bash
curl -sN http://localhost:8200/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"List the files in /tmp and tell me what you see."}]}'
```

You will see chunks arrive as the agent works:

```
data: {...,"delta":{"role":"assistant"},...}                           # stream opened

data: {...,"x_agenticore_event_type":"tool_use",
       "delta":{"tool_calls":[{"function":{"name":"Bash",...}}]}}       # agent calls Bash

data: {...,"x_agenticore_event_type":"tool_result",
       "delta":{"content":"file1\nfile2\n..."}}                         # shell output streams back

data: {...,"delta":{"content":"I see two files: ..."}}                  # agent's reply

data: {...,"delta":{},"finish_reason":"stop","usage":{...}}             # done

data: [DONE]
```

## 4. Pretty-printed viewer

Pipe through this one-liner to see the chunks labeled in order:

```bash
curl -sN http://localhost:8200/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"what files are in /tmp"}]}' \
  | python3 -c '
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("data: "): continue
    p = line[6:]
    if p == "[DONE]":
        print("[DONE]"); continue
    d = json.loads(p); ch = d["choices"][0]; delta = ch.get("delta", {})
    et = ch.get("x_agenticore_event_type")
    if delta.get("role") == "assistant": print("[open]")
    elif ch.get("finish_reason") == "stop": print(f"[stop] usage={d.get(\"usage\")}")
    elif et == "thinking": print(f"[thinking] {delta.get(\"content\",\"\")[:200]}")
    elif et == "tool_use":
        tc = delta["tool_calls"][0]["function"]
        print(f"[tool_use] {tc[\"name\"]}({tc.get(\"arguments\",\"\")[:100]})")
    elif et == "tool_result": print(f"[tool_result] {delta.get(\"content\",\"\")[:200]}")
    elif "content" in delta: print(f"[text] {delta[\"content\"]}")
'
```

## 5. Toggle visibility mid-conversation

```bash
# Hide tool deltas only — still see thinking + text
curl -sN http://localhost:8200/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"/hide-tools"}]}'

# Reset everything
curl -sN http://localhost:8200/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"/hide-all"}]}'

# Check current config for this agent
curl -sN http://localhost:8200/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"/stream-status"}]}'
```

See the full [slash token reference]({% link reference/sse-streaming.md %}#slash-tokens-visibility-toggles).

## 6. Auditable verification

To get a full cross-layer verification report (SSE + Redis + transcript all agree), run the audit script:

```bash
cd /path/to/agenticore
./tests/smoke/verify_streaming_pipeline.sh streaming-test
```

You'll get a markdown report like:

```
# SSE Pipeline Audit — **PASS**

- run_id: `20260413-212713-i8syg`
- agent: `streaming-test`
- correlation_uuid: `...`
- marker: `SSE-AUDIT-...`

## Marker propagation
- ✓ sse_tool_use
- ✓ sse_tool_result
- ✓ redis_tool_use
- ✓ redis_tool_result
- ✓ transcript_tool_use
- ✓ transcript_tool_result

## Consistency
- ✓ sse_tool_use_count_matches_redis
- ✓ sse_tool_result_count_matches_redis
- ✓ redis_tool_use_count_matches_transcript
- ✓ redis_tool_result_count_matches_transcript
```

Artifacts land in `/tmp/sse-audit/<run-id>/` for later review. See [SSE Streaming reference]({% link reference/sse-streaming.md %}#auditing-a-live-agent) for details.

## Propagating to all agents

`:dev` updates don't auto-rollout (ArgoCD image-updater is currently disabled). To pull the latest image onto an agent:

```bash
kubectl rollout restart -n anton-dev statefulset/<agent-name>
kubectl rollout status  -n anton-dev statefulset/<agent-name>
```

Verify the new image SHA matches what's on GHCR:

```bash
# What's on GHCR
gh api "/orgs/The-Cloud-Clock-Work/packages/container/agenticore/versions?per_page=3" \
  --jq '.[] | select(.metadata.container.tags[]? == "dev") | .name' | head -1

# What the pod is running
kubectl get pod <agent>-0 -n anton-dev \
  -o jsonpath='{.status.containerStatuses[?(@.name=="agenticore")].imageID}'
```

## Non-streaming still works

If you set `stream: false`, you get a regular OpenAI `chat.completion` JSON response — no changes there. Streaming is opt-in per request.

## What if I see only `role_open` + `stop` + `[DONE]`?

The pipeline is half-broken. Run the audit script — it will tell you which layer regressed:

```bash
./tests/smoke/verify_streaming_pipeline.sh <agent>
```

Common causes:
- Pod runs an old image that predates the feature — rollout restart
- `event_relay.py` missing from the agentihooks PVC — agentihooks sync hasn't completed
- `settings.json` doesn't wire the hooks — initializer didn't run

See the [fail modes table]({% link reference/sse-streaming.md %}#fail-modes-and-diagnostics) for the full matrix.
