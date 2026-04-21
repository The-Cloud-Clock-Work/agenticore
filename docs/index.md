---
title: Documentation
nav_order: 2
has_children: true
---

# Agenticore Documentation

**Two modes, one binary.** Agenticore is a production-grade Claude Code runner that runs in two distinct shapes — switched at runtime by a single environment variable.

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

## Pick a mode

| | **Fleet mode** _(default)_ | **Agent mode** _(`AGENT_MODE=true`)_ |
|---|---|---|
| **What it does** | Accepts coding tasks, clones repos, runs Claude in worktrees, opens PRs | Loads a customized Claude agent package and exposes it as a chat completion endpoint |
| **API surface** | `/jobs` REST · `run_task` MCP · `agenticore run` CLI | `/v1/chat/completions` — fully OpenAI-compatible, streaming and non-streaming |
| **Lifecycle** | Per-job clone + worktree, discarded after PR | Long-lived agent identity loaded once at startup |
| **Output** | A pull request, an OTEL trace | Live SSE deltas as `chat.completion.chunk` JSON, full transcript on disk |
| **Drop-in for** | CI/CD pipelines, MCP-aware editors, "fix this" bots | LibreChat, OpenWebUI, LiteLLM, any OpenAI SDK client |

Both modes share the **same binary**, **same Docker image**, **same Helm chart**, **same profile system**, **same Redis+file fallback**, and **same OTEL trace pipeline**.

---

## Why it matters

You have Claude Code. You want it to do work for you programmatically. You have two shapes the work tends to take:

1. **Headless coding tasks across repos** — "fix the auth bug", "add tests", "refactor this module". A fleet that accepts these, clones the right repo, runs Claude in a clean worktree, and opens a PR. → **Fleet mode**.

2. **A customized Claude agent your other tools can talk to** — a personal assistant, a domain expert, exposed as an OpenAI-compatible endpoint so LibreChat / OpenWebUI / LiteLLM / any OpenAI SDK client can drop it in as a "model", with **real-time streaming** of the agent's thinking, tool calls, and answers. → **Agent mode**.

Agenticore is one binary that does both. Profiles, hooks, MCP whitelists, Redis state, OTEL traces, Helm chart — all shared between the two modes.

---

## The agent-mode killer feature: real-time, fully auditable streaming

In agent mode, agenticore exposes `/v1/chat/completions` with `stream=true` and pipes claude's stdout directly through to the client as live OpenAI-format SSE deltas. **Thinking blocks stream token-by-token** as the model generates them. Tool calls and results stream **live**. Nothing is buffered to the end of the turn.

The streaming hot path runs `claude --output-format stream-json --verbose --include-partial-messages`, reads `proc.stdout` line-by-line, and dispatches each event into the appropriate SSE chunk shape. No transcript polling, no Redis indirection, no JSONL flush race.

Visibility is controlled by **deterministic slash tokens** stripped server-side before claude ever sees the prompt:

| Token | Effect |
|---|---|
| `/show-thinking` / `/hide-thinking` | Toggle thinking visibility |
| `/show-tools` / `/hide-tools` | Toggle tool_use + tool_result visibility |
| `/show-all` / `/hide-all` | Toggle everything |
| `/stream-status` | Return current config inline as a meta SSE event |

Toggles are sticky per agent in Redis (no TTL) with a file fallback, multi-turn aware, and **toggle-only requests return inline meta SSE without spawning claude** — zero token cost.

Every visible event reaches three observation surfaces simultaneously: (1) the wire (OpenAI SSE chunks), (2) claude's transcript JSONL on disk, and (3) optionally the Redis bus for cross-process subscribers. Cross-validate all three with the bundled audit script `tests/smoke/verify_streaming_pipeline.sh <agent>`.

→ Full reference: [SSE Streaming](reference/sse-streaming.md) · [Self-test walkthrough](getting-started/test-streaming.md)

---

## Deploy anywhere

| Mode | When to use |
|------|-------------|
| Standalone | Development, single-machine workloads |
| Docker Compose | Self-hosted, single-host production |
| Kubernetes (Helm) | Multi-pod, autoscaling, shared repo cache, per-agent StatefulSets |

---

## Getting Started

- [Quickstart](getting-started/quickstart.md) — Install, start the server, submit your first job
- [Connecting Clients](getting-started/connecting-clients.md) — MCP, REST, and CLI client setup
- [Test Streaming](getting-started/test-streaming.md) — Port-forward an agent pod and watch thinking + tool calls stream live

## Architecture

- [Architecture Internals](architecture/internals.md) — Modules, data flow, Redis+file fallback, repo caching
- [Dual Interface](architecture/dual-interface.md) — MCP + REST ASGI routing and auth middleware
- [Profile System](architecture/profile-system.md) — Directory-based profiles, agentihooks integration, materialization
- [Job Execution](architecture/job-execution.md) — Runner pipeline, lifecycle state machine, auto-PR, OTEL
- [Agent Mode](architecture/agent-mode.md) — Package-based agents, completion API, SSE streaming pipeline

## Deployment

- [Docker Compose](deployment/docker-compose.md) — Multi-service stack, volumes, networking
- [Kubernetes](deployment/kubernetes.md) — StatefulSet, shared RWX PVC, KEDA autoscaling, graceful drain
- [OTEL Pipeline](deployment/otel-pipeline.md) — Collector config, PostgreSQL sink, Langfuse traces
- [Releases and CI/CD](deployment/releases.md) — Versioning, tests, linting, self-update

## Reference

- [SSE Streaming](reference/sse-streaming.md) — Real-time thinking + tool deltas, slash token toggles, event schema, diagnostics, milestones
- [API Reference](reference/api-reference.md) — MCP tools + REST endpoints with schemas
- [CLI Commands](reference/cli-commands.md) — All CLI subcommands with flags and examples
- [Configuration](reference/configuration.md) — All env vars, YAML config, file paths
