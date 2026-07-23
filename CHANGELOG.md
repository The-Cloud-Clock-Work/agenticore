# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Microsoft Teams connector** (`agenticore/connectors/teams.py`) — a third transport on the
  shared `ProgressSink` pipeline, alongside Telegram and the SSE `/v1/chat/completions` path.
  Teams is push-based, so `server.py` registers an inbound `POST /api/messages` route on the
  existing ASGI app (no second listener) when `TEAMS_APP_ID` + `TEAMS_APP_PASSWORD` are set.
  Teams has no native reasoning panel, so — gated by the per-agent `stream_config` under a
  Teams-scoped agent id (`{AGENTIHUB_AGENT}:teams`) — thinking and tool activity render as
  ordinary titled chat messages (`**Reasoning:**`, `**Tool call:**`, `**Tool result:**`).
  Inbound Bot Framework JWT validation uses `PyJWT` + JWKS and the outbound token uses `httpx`
  OAuth2 client-credentials, both already core deps — so the connector needs no optional extra
  and no `botbuilder` SDK. v1 is 1:1 DMs, owner-filtered on `from.aadObjectId`; native token
  streaming (the `streaminfo` protocol) is planned v2. See `docs/reference/teams-connector.md`.

### Fixed

- **Stray error message on a successful retry** (all connectors). `AgentExecutor._run_subprocess`
  dispatched the terminal `on_error` on every attempt, so a retryable failure on an early attempt
  posted a durable "error" to the connector chat immediately before the retry's `on_final` landed.
  The terminal `on_error` is now withheld when `execute()` will retry the attempt
  (`_terminal_error_suppressed`), so a retried-then-successful turn shows only the answer.
- **Teams rendering + delivery robustness.** Tool-call/tool-result messages now wrap content in a
  dynamic backtick fence (longer than any run inside), so tool output that itself contains ```` ``` ````
  (e.g. reading a markdown file) no longer breaks rendering. Outbound messages are chunked by UTF-8
  byte budget on newline boundaries (not raw char index), so multibyte/long answers don't trip the
  ~28 KB payload cap or split mid-span. The typing indicator is resent every ~4s for the duration of
  a turn (it otherwise fades in ~3s). A turn that produces no final answer — or whose final send
  fails — now posts a guaranteed fallback so the user is never left with silence (delivery is tracked
  by confirmed send, not by attempt).

### Security

- **Teams connector hardening.** The outbound Bot Framework bearer token is sent only to the exact
  Teams service host `smba.trafficmanager.net` or Microsoft-owned `*.botframework.com` — the whole
  `*.trafficmanager.net` zone is NOT trusted (any Azure customer can register a name there), closing
  a token-exfiltration/SSRF vector via a forged `serviceUrl`. `authenticate_request` no longer raises
  on a JWKS-bootstrap failure (clean 401, not a 500 that downs the connector); non-`msteams` channels
  and channel/group conversations are refused (1:1-only, so reasoning/tool output can't leak to an
  unintended audience); turns are serialized per conversation (concurrent turns shared a `--resume`
  session + Redis event stream and would collide); the public `/api/messages` body is bounded (256 KB)
  and time-bounded (10 s, anti-slowloris) before parsing; and `<at>…</at>` mention markup is stripped
  from inbound text.
- **PyJWT bumped to `>=2.13.0`** (from `>=2.8` / pinned `2.11.0`) to fix
  [CVE-2026-48524](https://github.com/advisories/GHSA-fhv5-28vv-h8m8) — `PyJWKClient` made
  unbounded JWKS refetches on attacker-controlled unknown `kid` values, a DoS reachable on the
  public Teams webhook.

## [1.8.0] - 2026-07-14

### Changed

- **`agenticore agents` is local-first; Kubernetes is now opt-in.** The command previously ran
  `kubectl get pods --all-namespaces` on every launch, which made it unusable on a machine with
  no cluster and hard-wired K8s as *the* backend. With K8s disabled (the new default) no
  `kubectl` process is spawned at all and no K8s chrome is rendered — `discover_pods()`
  short-circuits before touching `subprocess`. Local AgentiHub agents are always discovered.
  This keeps room for other backends (Fargate/ECS) to be added as peers rather than exceptions.

### Added

- **K8s backend toggle** — `--k8s` / `--no-k8s` and `--namespace ns-a,ns-b` on
  `agenticore agents`; `AGENTICORE_K8S_ENABLED` / `AGENTICORE_K8S_NAMESPACES` env vars; and a
  persisted `{"k8s": {"enabled": …, "namespaces": […]}}` block in `~/.agenticore/state.json`,
  written by the new `k` key in the TUI. Precedence: CLI flag > env > state.json > off.
  A namespace named *on the CLI* implies `--k8s`; one from the env or state.json does not —
  ambient config must not resurrect K8s for an operator who never asked for it.
- **Namespace-scoped discovery** — each configured namespace is queried separately, since
  `kubectl` honours only the last `-n`. No namespaces configured = all-namespaces.
- **Local agents show real descriptions and capabilities**, read best-effort from the package's
  `command.yml` (the same manifest agentibridge reads for A2A capability routing). The TUI filter
  now matches on name, description, *and* capability. A missing or malformed manifest degrades to
  empty fields — authoring a package can never break discovery.
- **A corrupt `state.json` degrades instead of crashing or misfiring.** Invalid JSON, a non-dict
  `k8s` section, a scalar where `namespaces` should be a list, and a string-typed `"enabled":
  "false"` are all tolerated — the last of these matters because a naive truthy-cast would read
  the string `"false"` as `True` and silently switch Kubernetes *on*.
- `agents_tui.py` test coverage (66 tests) — it previously had none.

### Fixed

- **TUI crash on `/filter`** — `_render_list` filtered local agents on `LocalAgent.description`,
  a field the dataclass did not have, raising `AttributeError` the moment an operator typed a
  filter while any local agent was listed. The field now exists and is populated.
- **`--headless list` can no longer be misread.** It reports `{"k8s": {"enabled": …}}`, so an AI
  or script can distinguish "no pods found" from "the K8s backend is off". Pod actions
  (`chat`/`job`/`sync`/`health`) exit `2` with an actionable message instead of a confusing
  "pod not found" when K8s is disabled.
- **Live Chat targeted the wrong namespace** — the `kubectl exec` behind the TUI's Live Chat
  action omitted `-n <namespace>`, so it ran against whatever namespace the current kubectl
  context happened to point at.
- **CI: the push-side test gate had been decorative since May.** Every `push`-triggered `Tests`
  run failed with `ruff: command not found` / `pytest: command not found` (exit 127) while every
  `pull_request` run passed — the workflow routes pushes to a self-hosted runner where pip's
  console scripts land outside `PATH`, so the install step succeeded and the next step could not
  find the binary it had just installed. Invoking via `python -m` is runner-agnostic. Nothing
  merged via push in that window was actually tested by CI.

## [1.4.0] - 2026-04-17

### Added
- **4-tier conversation persistence** for `/v1/chat/completions` — sticky sessions via `X-Conversation-Id` header with content-hash fallback
- **`--resume` / `--session-id` wiring** — multi-turn continuity passed through to Claude subprocess

### Fixed
- `sem.locked()` public API compatibility
- `dev_mode` parsing from env/config
- Dangling `--allowedTools` flag on subprocess spawn

### Changed
- `POST /completions` emits deprecation warning — use `/v1/chat/completions`

### Docs
- `conversation-persistence.md` — full session-resume reference
- `a2a-conventions.md` — agent-to-agent communication conventions

---

## [1.3.1] - 2026-04-15

### Fixed
- Return valid OpenAI format for non-streaming `/stream-status` responses

### Changed
- Default stream visibility is now `show_all` — thinking and tool events on by default

---

## [1.3.0] - 2026-04-14

### Added
- **Real-time SSE streaming** via `--output-format stream-json`
- **Pseudo-slash tokens** — `/show-thinking`, `/hide-thinking`, `/show-tools`, `/hide-tools`, `/show-all`, `/hide-all`, `/stream-status`
- **`reasoning_content` routing** — tool_use and tool_result events arrive as fenced markdown blocks in `delta.reasoning_content` (renders as LibreChat collapsible panels)
- **Sticky per-agent stream visibility config** — persisted in Redis with file fallback

### Removed
- `hook_notifier.py`, `notifications.py`, and the callback_url notification system

---

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

[1.8.0]: https://github.com/The-Cloud-Clockwork/agenticore/compare/v1.7.0...v1.8.0
[1.4.0]: https://github.com/The-Cloud-Clockwork/agenticore/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/The-Cloud-Clockwork/agenticore/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/The-Cloud-Clockwork/agenticore/compare/v0.11.0...v1.3.0
[0.1.1]: https://github.com/The-Cloud-Clockwork/agenticore/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/The-Cloud-Clockwork/agenticore/releases/tag/v0.1.0
