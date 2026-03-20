# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.11.0] - 2026-03-20

### Changed
- **Worktrees moved to local disk** — worktrees now created under `AGENTICORE_WORKTREE_ROOT` (default: `~/.agenticore/worktrees/`), NOT on NFS. In Kubernetes, backed by emptyDir volume at `/app/worktrees`. Repos remain on shared FS at `/shared/repos/`.
- **`wait` parameter removed from `run_task`** — all jobs are fire-and-forget. Poll with `get_job(job_id)`.
- **Memory bumped to 4 CPU / 4Gi** — supports 10+ concurrent Claude processes (~320Mi each).

### Added
- **Two-phase worktree workflow** — `prepare_worktree(repo_url, base_ref)` returns a `worktree_id` that can be passed to `run_task(worktree_id=...)` to skip clone+worktree creation.
- **`get_worktree(worktree_id)`** — inspect a prepared worktree by ID.
- **`list_worktrees`** — list all worktrees with age, size, and push status.
- **`cleanup_worktrees`** — remove worktrees by path or age threshold.
- **SIGCHLD fix** — `preexec_fn=_reset_sigchld` in all subprocess calls prevents signal handler inheritance issues.
- **Stale worktree cleanup** — ephemeral worktrees on local disk are cleaned up automatically.
- **`asyncio.to_thread`** — blocking git operations wrapped for async compatibility.

### Verified
- 10 parallel jobs on single pod, peak 1018m CPU / 789Mi memory.

## [0.1.2] - 2026-03-03

### Changed

- **docs(profile-system)**: Document agentihub direct provisioning — agenticore clones agentihub and copies agent packages directly via `agent_mode/initializer.py`. No intermediate build step.

## [0.1.1] - 2025-06-15

### Added
- OTEL collector configuration for telemetry pipeline
- Smoke test workflow with Langfuse and Anthropic endpoint probes
- Cloudflare Access support for LiteLLM proxy routing

### Fixed
- Smoke test gracefully skips Langfuse on CF Access 302 responses
- Claude CLI routing through LiteLLM proxy with CF Access headers

## [0.1.0] - 2025-06-01

### Added
- FastMCP server with 5 tools (`run_task`, `get_job`, `list_jobs`, `cancel_job`, `list_profiles`)
- REST API endpoints alongside MCP tools
- Profile system — directory-based packages with `profile.yml` and `.claude/` config
- Default profiles bundled in `defaults/profiles/`
- Git clone/fetch with flock-based locking (`repos.py`)
- Job store with Redis + file-based fallback (`jobs.py`)
- Claude subprocess runner with OTEL environment injection (`runner.py`)
- Smart router with code fast-path and AI fallback (`router.py`)
- Auto-PR creation (git push + `gh pr create`) (`pr.py`)
- YAML config loader with environment variable overrides (`config.py`)
- CLI tool (`agenticore version`, `agenticore status`, `agenticore serve`)
- Docker support with full-stack compose (agent + Redis + sidecars)
- CI/CD workflows: test, build, release, docker-publish, publish-pypi, docs-audit, smoke-test
- Documentation site with 16 files

[0.1.1]: https://github.com/The-Cloud-Clock-Work/agenticore/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/The-Cloud-Clock-Work/agenticore/releases/tag/v0.1.0
