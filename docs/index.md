---
title: Documentation
nav_order: 2
has_children: true
---

# Agenticore Documentation

Agenticore is a Claude Code runner and orchestrator that manages job lifecycle,
repo cloning, profile-based execution, auto-PR creation, and OTEL observability.

```
Request (MCP / REST / CLI)
    |
    v
+---+---+     +--------+     +----------+     +--------+     +--------+
| Router +---->| Clone  +---->| Claude   +---->| Auto-PR+---->| Job    |
|        |     | repo   |     | --worktree|    | gh pr  |     | result |
+--------+     +--------+     | -p "task"|     | create |     | Redis  |
                              +----+-----+     +--------+     | + file |
                                   |                          +--------+
                                   v
                              +----+-----+
                              | OTEL     |
                              | Collector|
                              | -> PG    |
                              +----------+
```

## Getting Started

- [Quickstart](getting-started/quickstart.md) — Install, start the server, submit your first job
- [Connecting Clients](getting-started/connecting-clients.md) — MCP, REST, and CLI client setup

## Architecture

- [Architecture Internals](architecture/internals.md) — Modules, data flow, Redis+file fallback, repo caching
- [Dual Interface](architecture/dual-interface.md) — MCP + REST ASGI routing and auth middleware
- [Profile System](architecture/profile-system.md) — Profile YAML to CLI flags, templates, routing
- [Job Execution](architecture/job-execution.md) — Runner pipeline, lifecycle state machine, auto-PR, OTEL

## Deployment

- [Docker Compose](deployment/docker-compose.md) — 4-service stack, volumes, networking
- [Kubernetes](deployment/kubernetes.md) — Helm chart, values, PVCs, secrets
- [OTEL Pipeline](deployment/otel-pipeline.md) — Collector config, PostgreSQL sink
- [Releases and CI/CD](deployment/releases.md) — Versioning, tests, linting, self-update

## Reference

- [CLI Commands](reference/cli-commands.md) — All 9 CLI subcommands with flags and examples
- [Configuration](reference/configuration.md) — All env vars, YAML config, file paths
- [API Reference](reference/api-reference.md) — 5 MCP tools + 6 REST endpoints with schemas
