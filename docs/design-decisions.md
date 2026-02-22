# Design Decisions

## 1. Claude Code Does the Heavy Lifting

Claude Code now supports `--worktree` and native OTEL. We don't reinvent these.
Agenticore orchestrates jobs and lets Claude do the work.

## 2. Decoupling Through the Database

Everything lands in PostgreSQL via OTEL. You read the DB to know what the system
is doing. Plug Grafana, Langfuse, or any UI on top. No custom dashboards.

## 3. Profile System Over System Prompt Injection

Profiles map to Claude CLI flags. The repo's own CLAUDE.md stays untouched.
Profiles add behavior on top via `--append-system-prompt` and `--settings`.

## 4. Redis + File Fallback

Same pattern as AgentiBridge. Try Redis first, fall back to filesystem.
Jobs stored as Redis hashes or `~/.agenticore/jobs/{id}.json`.

## 5. No Custom Worktree Code

Claude `--worktree` handles worktree creation and cleanup. Agenticore just
runs `claude --worktree` with `cwd` set to the repo directory.

## 6. Auto-PR as Post-Processing

When `profile.auto_pr: true` and exit_code == 0:
1. Push the worktree branch
2. Create PR via `gh pr create`
3. Store PR URL in job artifacts

## 7. Code Router Default, AI Fallback

Code fast-path handles the common case (explicit profile or default).
AI router is the selling point for ambiguous requests but isn't required.

## 8. Dropped from Old Agenticore

- 12+ AWS integrations (not a runner concern)
- RAG search (AgentiBridge has this)
- Session registry + correlation maps (simple job → session_id)
- S3 sync/hot-reload (local YAML config)
- Terraform, sidecar containers, hook event system
- Custom worktree management, custom observability, metrics/dashboards
