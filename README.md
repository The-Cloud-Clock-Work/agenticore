# Agenticore

**Two modes, one binary.** Run a fleet of Claude Code agents that clone repos and ship PRs — or expose any customized Claude Code agent as a real-time, OpenAI-compatible chat completion endpoint with **token-by-token thinking and tool deltas**. Flip between modes with one environment variable.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/The-Cloud-Clock-Work/agenticore/blob/main/LICENSE)
[![Tests](https://github.com/The-Cloud-Clock-Work/agenticore/actions/workflows/test.yml/badge.svg)](https://github.com/The-Cloud-Clock-Work/agenticore/actions/workflows/test.yml)
[![Docker](https://img.shields.io/docker/v/tccw/agenticore?label=Docker%20Hub)](https://hub.docker.com/r/tccw/agenticore)
[![Helm](https://img.shields.io/badge/Helm-GHCR-blue)](https://ghcr.io/the-cloud-clock-work/charts/agenticore)
[![PyPI](https://img.shields.io/pypi/v/agenticore)](https://pypi.org/project/agenticore/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://python.org)

```
                          ┌─── AGENT_MODE=false (default) ────────────┐
                          │  FLEET MODE — Orchestrator                 │
                          │  Submit a task, get a PR                   │
                          │                                            │
   MCP / REST / CLI ─────►│  clone repo ──► bespoke worktree           │
                          │       │              │                     │
                          │       └──► claude -p "<task>" ──► auto-PR  │
                          │                              └──► OTEL     │
                          │  KEDA-scaled fleet • work-stealing queue   │
   ┌─────────────┐        └────────────────────────────────────────────┘
   │ agenticore  │
   │   binary    │
   └─────────────┘        ┌─── AGENT_MODE=true ────────────────────────┐
                          │  AGENT MODE — Customized agent endpoint    │
                          │  Drop-in OpenAI chat completion server     │
                          │                                            │
   OpenAI-compatible ────►│  load agent package (system prompt, MCP    │
   chat clients           │    servers, hooks, skills, identity)       │
   (LibreChat,            │                                            │
    OpenWebUI,            │  POST /v1/chat/completions stream=true     │
    LiteLLM,              │       │                                    │
    custom UI,            │       └─► live SSE deltas:                 │
    raw curl -N)          │            thinking_delta (token-by-token) │
                          │            tool_use + tool_result          │
                          │            assistant text                  │
                          │                                            │
                          │  Sticky slash toggles per agent            │
                          │  Fully auditable — wire/disk/Redis layers  │
                          └────────────────────────────────────────────┘
```

---

## Pick a mode

| | **Fleet mode** _(default)_ | **Agent mode** _(`AGENT_MODE=true`)_ |
|---|---|---|
| **What it does** | Accepts coding tasks, clones repos, runs Claude Code in bespoke worktrees, opens PRs | Loads a pre-configured Claude Code agent package and exposes it as a chat completion endpoint |
| **API surface** | `/jobs` REST · `run_task` MCP tool · `agenticore run` CLI | `/v1/chat/completions` — fully OpenAI-compatible, streaming and non-streaming |
| **Lifecycle** | Per-job clone + worktree, discarded after PR | Long-lived agent identity loaded once at container startup |
| **Scaling** | KEDA on Redis queue depth — N pods steal jobs from one queue | One StatefulSet per agent identity; scale horizontally per agent |
| **Output** | A pull request, an OTEL trace, a job result in Redis | Live SSE deltas as `chat.completion.chunk` JSON, full transcript on disk |
| **Drop-in for** | CI/CD pipelines, MCP-aware editors, internal "fix this" bots | LibreChat, OpenWebUI, LiteLLM model routing, any OpenAI SDK client |
| **Best for** | "We use Claude Code to refactor / fix / generate PRs across many repos" | "We want our chat clients to talk to a customized Claude agent over the OpenAI protocol" |

Both modes share the **same binary**, the **same Docker image**, the **same Helm chart**, the same profile system, the same Redis+file fallback, and the same OTEL trace pipeline. **You don't pick at install time. You pick at runtime with one environment variable.**

---

## Why agenticore

You have Claude Code. You want it to do work for you programmatically. You have two shapes the work tends to take:

1. **Headless coding tasks across repos** — "fix the auth bug", "add tests for the parser", "refactor this module". You want a fleet that accepts these, clones the right repo, runs Claude in a clean worktree, and opens a PR. → **Fleet mode**.

2. **A customized Claude agent your other tools can talk to** — a personal assistant, a domain expert, a finops bot, a docs writer — exposed as an OpenAI-compatible endpoint so LibreChat, OpenWebUI, your LiteLLM router, or any OpenAI SDK client can drop it in as a "model". With **real-time streaming** of the agent's thinking, tool calls, and answers — not buffered, not batched, not faked. → **Agent mode**.

Agenticore is one binary that does both. Profiles, hooks, MCP whitelists, Redis state, OTEL traces, Helm chart — all shared between the two modes. Your operations team learns one thing.

---

# 🟦 FLEET MODE

> Submit a task, get a PR. The original positioning.

```
MCP Client / REST Client / CLI
            │
            ▼
    ┌── Agenticore (Fleet Mode) ─────────────────────────────────┐
    │   Auth · Router · Job Queue                                │
    │                                                            │
    │   Clone repo ──► Bespoke worktree ──► claude -p "task"     │
    │   (cached)       (locked branch)      (cwd = worktree)     │
    │                                         │                  │
    │                                         ▼                  │
    │                                   Auto-PR (gh)             │
    │                                   Job result → Redis       │
    └──────────────────────┬─────────────────────────────────────┘
                           │
                    OTEL Collector
                    → Langfuse / PostgreSQL
```

- Accepts tasks from **MCP clients, REST, or CLI** — same API surface, one port
- **Clones and caches repos**, serializes concurrent access with distributed locks
- Creates **bespoke worktrees** — locked before Claude starts, deterministic branch names
- **Applies execution profiles** — installed into `~/.claude/` at startup via agentihooks
- Spawns `claude -p "<task>"` in the worktree and **opens a PR when it succeeds**
- Ships **full OTEL traces** (prompts, tool calls, token counts) to Langfuse / PostgreSQL
- **KEDA autoscaling** on Redis queue depth + **graceful drain** on pod shutdown

### Quickstart

```bash
# Set credentials
export ANTHROPIC_AUTH_TOKEN=sk-ant-...
export GITHUB_TOKEN=ghp_...

# Start the server
agenticore serve

# Submit a task and wait for the PR URL
agenticore run "fix the null pointer in auth.py" \
  --repo https://github.com/org/repo \
  --wait
```

### REST

```bash
# Submit a job (async — returns immediately with job ID)
curl -X POST http://localhost:8200/jobs \
  -H "Content-Type: application/json" \
  -d '{"task":"fix the auth bug","repo_url":"https://github.com/org/repo"}'

# Submit and wait
curl -X POST http://localhost:8200/jobs \
  -H "Content-Type: application/json" \
  -d '{"task":"fix the auth bug","repo_url":"https://github.com/org/repo","wait":true}'

# Inspect
curl http://localhost:8200/jobs/{job_id}
curl "http://localhost:8200/jobs?limit=10&status=running"
curl -X DELETE http://localhost:8200/jobs/{job_id}
```

### MCP tools (fleet mode)

| Tool | Description |
|------|-------------|
| `run_task` | Submit a task for Claude Code execution |
| `get_job` | Get status, output, and PR URL for a job |
| `list_jobs` | List recent jobs |
| `cancel_job` | Cancel a running or queued job |
| `list_profiles` | List available execution profiles |
| `plan_task` | Create a read-only implementation plan |
| `execute_plan` | Execute a ready plan as a coding job |
| `list_worktrees` | List all worktrees with age, size, branch, push status |
| `cleanup_worktrees` | Remove specific worktrees (unlock + delete) |

Connect any MCP client at `http://localhost:8200/mcp` (Streamable HTTP) or `/sse` (legacy SSE).

---

# 🟩 AGENT MODE

> One environment variable. Now you have a customized Claude agent talking the OpenAI protocol with real-time thinking + tool streaming.

```
        AGENT_MODE=true + AGENT_MODE_PACKAGE_DIR=./my-agent-package
                                        │
                                        ▼
  ┌── Agenticore (Agent Mode) ──────────────────────────────────────┐
  │                                                                 │
  │   Load package once at startup:                                 │
  │     ├─ system.md (identity, instructions)                       │
  │     ├─ .claude/ (settings, hooks, skills, agents)               │
  │     └─ .mcp.json (tool servers this agent can call)             │
  │                                                                 │
  │   POST /v1/chat/completions stream=true                         │
  │     │                                                           │
  │     ├─ strip slash tokens (server-side, deterministic)          │
  │     ├─ load sticky visibility config from Redis                 │
  │     ├─ spawn claude --output-format stream-json                 │
  │     │                  --include-partial-messages               │
  │     ├─ read claude stdout line-by-line                          │
  │     │     thinking_delta  → delta.reasoning_content (live)      │
  │     │     text_delta      → delta.content (live)                │
  │     │     tool_use_block  → ```tool_use:NAME fenced block       │
  │     │     tool_result     → ```tool_result fenced block         │
  │     └─ flush each chunk to the open HTTP connection             │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

**Drop-in for any OpenAI-compatible client.** Because the endpoint speaks `/v1/chat/completions` and emits standard `chat.completion.chunk` JSON over SSE, you can register an agenticore-backed agent as an "OpenAI custom model" inside:

- **LibreChat** — add as a custom OpenAI endpoint, pick from the model dropdown
- **OpenWebUI** — same pattern
- **LiteLLM** — register as `openai/<agent>` with `api_base=http://<agent>:8200/v1`, then route any LiteLLM client at it
- **OpenAI SDK** (Python, JS, Go, Rust) — `OpenAI(base_url="http://<agent>:8200/v1")` and call `chat.completions.create(...)` exactly like you would against `api.openai.com`
- **`curl -N`** — raw SSE works fine

### Killer features

- **Real-time SSE streaming, fully auditable, fully traceable.** Thinking blocks stream **token-by-token** as the model generates them. Tool calls and results stream **live** as the agent invokes them. Assistant text streams progressively. Nothing is buffered to the end of the turn. The streaming hot path reads claude's stdout directly via `--output-format stream-json --verbose --include-partial-messages` — no transcript polling, no Redis indirection, no JSONL flush race.
- **Thinking renders in `delta.reasoning_content`** — separate reasoning panel in reasoning-aware clients (LibreChat, OpenWebUI), with `x_agenticore_event_type="thinking"` for custom clients that want explicit tagging.
- **Tool calls render as fenced markdown blocks** — ` ```tool_use:NAME ` paired with ` ```tool_result ` below it. Deliberately **not** OpenAI's `delta.tool_calls` schema, which would make chat clients try to client-execute the tool and fail with "Tool not found".
- **Sticky per-agent visibility toggles** intercepted server-side, before claude ever sees the prompt:
  - `/show-thinking` / `/hide-thinking`
  - `/show-tools` / `/hide-tools`
  - `/show-all` / `/hide-all`
  - `/stream-status` (returns the current config inline as a meta SSE event)
- **Multi-turn aware** — toggle detection runs against the **last user message**, not the flattened history, so slash commands work on turn 2+. **Toggle-only requests** (e.g. just `/show-all`) return inline status without spawning claude — zero token cost.
- **Three-layer observation** — every visible event reaches (1) the client over the wire, (2) claude's transcript JSONL on disk, and (3) optionally the Redis bus (non-streaming path) for cross-process subscribers. Cross-validate all three with `tests/smoke/verify_streaming_pipeline.sh <agent>`.
- **Async completion queue** for fire-and-forget — `wait=false` pushes to Redis, a worker picks it up, poll `GET /completions/{uuid}`.
- **Session continuity** — resume a conversation across requests via the external correlation UUID.
- **Redis+file fallback** — works without Redis (inline execution, file-based state).

### Quickstart

```bash
# Start the server in agent mode pointing at your agent package
AGENT_MODE=true \
AGENT_MODE_PACKAGE_DIR=./my-agent-package \
AGENTICORE_TRANSPORT=sse \
agenticore serve

# Toggle visibility once (sticky per agent — persists in Redis)
curl -sN http://localhost:8200/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"/show-all"}]}'

# Now have a real conversation — watch thinking tokens + tool calls stream live
curl -sN http://localhost:8200/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"sonnet","stream":true,"messages":[
        {"role":"user","content":"is 17077 prime? think hard, then list any files in /tmp"}
      ]}'

# Non-streaming JSON (no slash tokens needed)
curl -X POST http://localhost:8200/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"sonnet","messages":[{"role":"user","content":"hello"}]}'
```

### Drop into LibreChat

```yaml
# librechat.yaml
endpoints:
  custom:
    - name: "Agenticore Agents"
      apiKey: "${LITELLM_API_KEY}"
      baseURL: "http://litellm.your-cluster.svc:4000/v1"
      models:
        fetch: true
      titleConvo: true
```

Register the agent in LiteLLM as a model pointing at the agenticore pod:

```bash
# Via LiteLLM admin (or the litellm_tools MCP)
model_name: my-agent
litellm_params:
  model: openai/my-agent
  api_base: http://my-agent.namespace.svc:8200/v1
```

Now `my-agent` shows up in LibreChat's model picker. Token-by-token thinking renders in the reasoning panel. Tool calls stream live as fenced markdown blocks.

### Drop into the OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://my-agent.namespace.svc:8200/v1", api_key="n/a")

stream = client.chat.completions.create(
    model="sonnet",
    stream=True,
    messages=[
        {"role": "user", "content": "/show-all explain how an OS scheduler works step by step"},
    ],
)
for chunk in stream:
    delta = chunk.choices[0].delta
    if reasoning := getattr(delta, "reasoning_content", None) or delta.model_dump().get("reasoning_content"):
        print(f"[think] {reasoning}", end="", flush=True)
    elif delta.content:
        print(delta.content, end="", flush=True)
```

Full reference: **[SSE Streaming docs](docs/reference/sse-streaming.md)** · **[Self-test walkthrough](docs/getting-started/test-streaming.md)** · **[Agent Mode architecture](docs/architecture/agent-mode.md)**

---

# Shared infrastructure (both modes)

Everything below applies to **both** Fleet mode and Agent mode. Same Docker image, same Helm chart, same env vars, same Redis schema.

## Install

```bash
pip install agenticore
```

Or from source:

```bash
git clone https://github.com/The-Cloud-Clock-Work/agenticore.git
cd agenticore
pip install -e .
```

## Profiles

Profiles are **directory packages** that configure how Claude Code runs. Each profile is a self-contained `.claude/` tree installed into `~/.claude/` at container startup by `agentihooks global`. Claude Code reads from `~/.claude/` by default.

```
<profiles-dir>/{name}/
├── profile.yml          ← Agenticore metadata (model, turns, auto_pr, timeout…)
├── .claude/
│   ├── settings.json    ← Hooks, tool permissions, env vars
│   ├── CLAUDE.md        ← System instructions for Claude
│   ├── agents/          ← Custom subagents
│   └── skills/          ← Custom slash-command skills
└── .mcp.json            ← MCP server config merged into the job
```

Profiles support inheritance via `extends:` and ship with the [`agentihooks`](https://pypi.org/project/agentihooks/) PyPI package (a pip dependency of agenticore). Personal overrides live in `~/.agenticore/profiles/`; set `AGENTICORE_AGENTIHOOKS_PATH` for a dev loopback against a local agentihooks checkout. Full reference: [Profile System docs](docs/architecture/profile-system.md).

## Helm (Kubernetes)

Production-ready Helm chart published to GHCR. Deploys a **StatefulSet** with a **shared RWX PVC** (NFS / EFS / Azure Files / Ceph) so all pods share the same repo cache and job state, with **KEDA autoscaling** on Redis queue depth and **graceful drain** on pod shutdown.

```
Internet ──► LoadBalancer :8200
                    │
     ┌──────────────▼──────────────────────────┐
     │  Agenticore StatefulSet (0..N pods)     │
     │  Work-stealing from Redis queue         │
     └──────────┬──────────────────────────────┘
                │                │
         ┌──────▼───────┐  ┌─────▼───────────┐
         │  Redis       │  │  Shared RWX PVC │
         │  jobs · locks│  │  /shared/       │
         │  KEDA queue  │  │  ├─ repos/      │
         └──────────────┘  │  ├─ jobs/       │
                           │  └─ job-state/  │
         KEDA ScaledObject └─────────────────┘
         watches Redis queue
```

```bash
# Create the secret
kubectl create secret generic agenticore-secrets \
  --from-literal=redis-url="redis://:password@redis:6379" \
  --from-literal=anthropic-api-key="sk-ant-..." \
  --from-literal=github-token="ghp_..."

# Install (fleet mode)
helm install agenticore \
  oci://ghcr.io/the-cloud-clock-work/charts/agenticore \
  --set storage.className=your-rwx-storage-class

# Install (agent mode)
helm install my-agent \
  oci://ghcr.io/the-cloud-clock-work/charts/agenticore \
  --set storage.className=your-rwx-storage-class \
  --set agentMode.enabled=true \
  --set agentMode.agentName=my-agent
```

Full Kubernetes guide: [Kubernetes Deployment](docs/deployment/kubernetes.md).

## Docker

```bash
# Local dev — full stack (Agenticore + Redis + PostgreSQL + OTEL Collector)
cp .env.example .env
docker compose up --build -d

# Production (fleet mode) — Agenticore only
docker run -d -p 8200:8200 \
  -e AGENTICORE_TRANSPORT=sse \
  -e ANTHROPIC_AUTH_TOKEN=sk-ant-... \
  -e REDIS_URL=redis://your-redis:6379/0 \
  -e GITHUB_TOKEN=ghp_... \
  tccw/agenticore

# Production (agent mode)
docker run -d -p 8200:8200 \
  -e AGENT_MODE=true \
  -e AGENTIHUB_AGENT=my-agent \
  -e AGENTICORE_TRANSPORT=sse \
  -e ANTHROPIC_AUTH_TOKEN=sk-ant-... \
  -e REDIS_URL=redis://your-redis:6379/0 \
  tccw/agenticore
```

## Authentication

Authentication is **optional**. When disabled, all endpoints are public.

```bash
# API keys — comma-separated for multiple
AGENTICORE_API_KEYS="key-1,key-2" agenticore serve
```

Pass the key via `X-Api-Key` header, `?api_key=...` query param, or `Authorization: Bearer ...`. The `/health` endpoint is always public.

Claude credentials resolved in order: `CLAUDE_CODE_OAUTH_TOKEN` → `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`. GitHub credentials: GitHub App (`GITHUB_APP_ID` + key + installation ID) → static `GITHUB_TOKEN` → none (public repos only).

## OTEL Observability

Every job (fleet mode) and every completion (agent mode) produces a Langfuse trace with spans for each Claude turn including prompts, tool calls, and token counts.

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
AGENTICORE_OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

The bundled `docker-compose.yml` includes an OTEL Collector pre-wired to push traces to Langfuse and PostgreSQL. Full setup: [OTEL Pipeline docs](docs/deployment/otel-pipeline.md).

## Key environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_MODE` | `false` | **The mode switch.** `true` enables agent mode |
| `AGENT_MODE_PACKAGE_DIR` | _(empty)_ | Path to the agent package (agent mode only) |
| `AGENTIHUB_AGENT` | _(empty)_ | Agent name to load from agentihub (agent mode) |
| `AGENTICORE_TRANSPORT` | `stdio` | `sse` for HTTP server, `stdio` for MCP pipe |
| `AGENTICORE_HOST` | `127.0.0.1` | Bind address |
| `AGENTICORE_PORT` | `8200` | Server port |
| `AGENTICORE_API_KEYS` | _(empty)_ | Comma-separated API keys (optional) |
| `ANTHROPIC_AUTH_TOKEN` | _(empty)_ | Anthropic API key (or use `CLAUDE_CODE_OAUTH_TOKEN`) |
| `REDIS_URL` | _(empty)_ | Redis URL — omit for file-based fallback |
| `GITHUB_TOKEN` | _(empty)_ | GitHub token for auto-PR (fleet mode) |
| `AGENTIHOOKS_PROFILE` | `coding` | Active profile (fleet mode) |
| `AGENTICORE_CLAUDE_TIMEOUT` | `3600` | Max claude runtime in seconds |
| `AGENTICORE_AGENTIHOOKS_PATH` | _(empty)_ | Dev loopback: `uv pip install -e <path>` over the PyPI agentihooks (wins over URL) |
| `AGENTICORE_AGENTIHOOKS_URL` | _(empty)_ | Override: clone ONCE + `uv pip install -e`. Default install is from PyPI. |
| `AGENTICORE_AGENTIHOOKS_BUNDLE_URL` | _(empty)_ | Git URL to clone the bundle content repo |
| `AGENTICORE_AGENTIHUB_URL` | _(empty)_ | Git URL for agentihub repo (agent mode) |
| `AGENTICORE_SHARED_FS_ROOT` | _(empty)_ | Shared FS root (Kubernetes mode) |

Full reference: [Configuration docs](docs/reference/configuration.md).

## CLI commands

| Command | Description |
|---------|-------------|
| `agenticore serve` | Start the server (fleet or agent mode based on env) |
| `agenticore run "<task>" --repo <url> [--wait]` | Submit a task (fleet mode) |
| `agenticore jobs` / `agenticore job <id>` | List / inspect jobs |
| `agenticore cancel <id>` | Cancel a running job |
| `agenticore profiles` | List execution profiles |
| `agenticore agents` | Interactive TUI — K8s pods + local agent packages |
| `agenticore agents --headless <action>` | Headless: `list`, `chat`, `job`, `sync`, `health`, `local` |
| `agenticore hooks sync [--target T]` | Clone/fetch profile sources |
| `agenticore agent --compose-up` | Bring up the local dev stack |
| `agenticore drain` | Drain pod before shutdown (Kubernetes) |
| `agenticore status` / `version` / `update` | Server health, version, self-update |

Full CLI reference: [CLI Commands](docs/reference/cli-commands.md).

---

## Documentation

### Get started
- [Quickstart](docs/getting-started/quickstart.md)
- [Connecting Clients](docs/getting-started/connecting-clients.md)
- [SSE Streaming self-test](docs/getting-started/test-streaming.md)

### Architecture
- [Profile System](docs/architecture/profile-system.md)
- [Agent Mode](docs/architecture/agent-mode.md)
- [Internals](docs/architecture/internals.md)
- [Job Execution](docs/architecture/job-execution.md)

### Deployment
- [Kubernetes](docs/deployment/kubernetes.md)
- [Docker Compose](docs/deployment/docker-compose.md)
- [OTEL Pipeline](docs/deployment/otel-pipeline.md)

### Reference
- [SSE Streaming](docs/reference/sse-streaming.md) — full chunk schema, slash tokens, fail-mode diagnostics
- [API Reference](docs/reference/api-reference.md)
- [CLI Reference](docs/reference/cli-commands.md)
- [Configuration](docs/reference/configuration.md)

---

## Development

```bash
pip install -e ".[dev]"

# Tests
pytest tests/unit -v -m unit --cov=agenticore

# Lint
ruff check agenticore/ tests/
ruff format --check agenticore/ tests/
```

### agentihooks dev loopback

`agentihooks` ships as a PyPI dependency of agenticore (`agentihooks>=1.8.0` in
`pyproject.toml`) and is pulled transitively when you `pip install agenticore`
or build the Docker image. You do **not** need to clone it to get a working
runtime. A pod restart is the upgrade path — no periodic re-sync watcher.

When you're iterating on agentihooks itself and want the mounted checkout to
be the live runtime, use one of the two override escape hatches:

| Env var | Behaviour |
|---|---|
| `AGENTICORE_AGENTIHOOKS_PATH=<local path>` | At boot: `uv pip install -e <path>` overlays the PyPI version. Ideal for docker-compose / k8s dev pods with the repo bind-mounted. **Wins over URL.** |
| `AGENTICORE_AGENTIHOOKS_URL=<git url>` (+ optional `_BRANCH`) | At boot: clone ONCE to `{SHARED_FS_ROOT}/agentihooks`, then `uv pip install -e`. For smoke-testing a fork or branch without a PyPI release. |
| *(neither set)* | Container runs the PyPI-installed version. Default. |

`docker-compose.dev.yml` wires the loopback out of the box:

```yaml
volumes:
  - /home/iamroot/dev/agentihooks:/opt/agentihooks-src
environment:
  AGENTICORE_AGENTIHOOKS_PATH: /opt/agentihooks-src
```

Edit the mounted checkout → re-run the server (or let Python re-import on
worker restart). The clone+watcher machinery from the old model is gone;
bundle and agentihub content repos retain their watchers. See
[`docs/reference/configuration.md`](docs/reference/configuration.md#agentihooks)
for full env-var semantics.

PRs welcome. The `feat/*` branches in the repo show recent work — the most recent landed feature is the token-by-token SSE streaming layer (`feat/stream-json-direct` → `dev` → `main` at `f440e3c`, released as `v1.3.0`).
