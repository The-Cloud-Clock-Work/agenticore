---
title: Documentation
nav_order: 2
has_children: true
---

# Agenticore Documentation

Agenticore is a **production-grade Claude Code runner and orchestrator**. It
manages the full job lifecycle — repo cloning, profile-based execution,
auto-PR creation, and OTEL observability — while Claude Code does the actual
coding work.

```
MCP Client / REST Client / CLI
            │
            ▼
    ┌── Agenticore ──────────────────────────────────────────────┐
    │   Auth · Router · Job Queue                                │
    │                                                            │
    │   Clone repo ──► Materialize profile ──► claude --worktree │
    │   (cached, distributed lock)   (.claude/ + .mcp.json)     │
    │                                         │                  │
    │                                         ▼                  │
    │                                   Auto-PR (gh)             │
    │                                   Job result → Redis       │
    └──────────────────────┬─────────────────────────────────────┘
                           │
                    OTEL Collector
                    → Langfuse / PostgreSQL
```

Deploy anywhere:

| Mode | When to use |
|------|-------------|
| Standalone | Development, single-machine workloads |
| Docker Compose | Self-hosted, single-host production |
| Kubernetes (Helm) | Multi-pod, autoscaling, shared repo cache |

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
- [Agent Mode](architecture/agent-mode.md) — Package-based agents, completion queue, notification streaming

## Deployment

- [Docker Compose](deployment/docker-compose.md) — 4-service stack, volumes, networking
- [Kubernetes](deployment/kubernetes.md) — StatefulSet, shared RWX PVC, KEDA autoscaling, graceful drain
- [OTEL Pipeline](deployment/otel-pipeline.md) — Collector config, PostgreSQL sink, Langfuse traces
- [Releases and CI/CD](deployment/releases.md) — Versioning, tests, linting, self-update

## Reference

- [CLI Commands](reference/cli-commands.md) — All 11 CLI subcommands with flags and examples
- [Configuration](reference/configuration.md) — All env vars, YAML config, file paths
- [API Reference](reference/api-reference.md) — 5 MCP tools + 6 REST endpoints with schemas
- [SSE Streaming](reference/sse-streaming.md) — Real-time thinking + tool deltas, slash token toggles, event schema, diagnostics
