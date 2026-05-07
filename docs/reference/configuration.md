---
title: Configuration
nav_order: 2
parent: Reference
---

# Configuration Reference

Agenticore loads configuration from a YAML file with environment variable overrides.

## Precedence

```
Environment variables  (highest)
        |
   YAML config file    (~/.agenticore/config.yml)
        |
   Built-in defaults   (lowest)
```

## YAML Config File

Default path: `~/.agenticore/config.yml`

```yaml
server:
  host: "127.0.0.1"
  port: 8200
  transport: sse
  api_keys:
    - "your-secret-key"

claude:
  binary: claude
  timeout: 3600
  # default_profile removed — use AGENTIHOOKS_PROFILE env var
  config_dir: ""

repos:
  root: ~/agenticore-repos
  max_parallel_jobs: 3
  job_ttl_seconds: 86400
  # Kubernetes / shared FS (optional):
  shared_fs_root: ""
  jobs_dir: ""
  pod_name: ""
  worktree_root: ""  # /app/worktrees in Kubernetes (emptyDir)

redis:
  url: "redis://localhost:6379/0"
  key_prefix: agenticore

otel:
  enabled: true
  endpoint: "http://otel-collector:4317"
  protocol: grpc
  log_prompts: false
  log_tool_details: true

github:
  token: ""

langfuse:
  host: "https://cloud.langfuse.com"
  public_key: ""
  secret_key: ""

agentihooks_url: ""
agentihooks_bundle_url: ""
agentihub_url: ""
```

## Environment Variables

### Server

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `AGENTICORE_HOST` | `server.host` | `127.0.0.1` | Bind address |
| `AGENTICORE_PORT` | `server.port` | `8200` | Listen port |
| `AGENTICORE_TRANSPORT` | `server.transport` | `sse` | Transport mode (`sse` or `stdio`) |
| `AGENTICORE_API_KEYS` | `server.api_keys` | (none) | Comma-separated API keys for auth |

### Claude

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `AGENTICORE_CLAUDE_BINARY` | `claude.binary` | `claude` | Path to Claude CLI binary |
| `AGENTICORE_CLAUDE_TIMEOUT` | `claude.timeout` | `3600` | Max seconds per job |
| `AGENTIHOOKS_PROFILE` | `agentihooks_profile` | `coding` | Active execution profile (set by agentihooks) |
| `AGENTICORE_CLAUDE_CONFIG_DIR` | `claude.config_dir` | (none) | **DEPRECATED** — Claude Code uses `~/.claude/` by default |
| `CLAUDE_CODE_HOME_DIR` | `claude.claude_home_dir` | `$HOME` | Safeguard: home dir root — Claude uses `$CLAUDE_CODE_HOME_DIR/.claude/` |

### Repos

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `AGENTICORE_REPOS_ROOT` | `repos.root` | `~/agenticore-repos` | Root directory for cloned repos |
| `AGENTICORE_MAX_PARALLEL_JOBS` | `repos.max_parallel_jobs` | `3` | Max concurrent jobs |
| `AGENTICORE_JOB_TTL` | `repos.job_ttl_seconds` | `86400` | Job TTL in seconds (24h default) |
| `AGENTICORE_SHARED_FS_ROOT` | `repos.shared_fs_root` | (none) | Shared RWX filesystem root (Kubernetes). When set, enables K8s mode: profiles are materialised to `/shared/jobs/{job-id}/` instead of the repo working dir. |
| `AGENTICORE_JOBS_DIR` | `repos.jobs_dir` | `~/.agenticore/jobs` | Override job JSON file directory. In K8s use `/shared/job-state`. |
| `AGENTICORE_POD_NAME` | `repos.pod_name` | `hostname` | Pod identity recorded on each job. Set from K8s Downward API (`metadata.name`). |
| `AGENTICORE_WORKTREE_ROOT` | `repos.worktree_root` | `~/.agenticore/worktrees` | Root directory for bespoke worktrees. In Kubernetes, use an emptyDir mount (e.g. `/app/worktrees`). Worktrees are ephemeral and live on local disk, NOT shared FS. |

### Redis

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `REDIS_URL` | `redis.url` | (none) | Redis connection URL |
| `REDIS_KEY_PREFIX` | `redis.key_prefix` | `agenticore` | Key namespace prefix |

### OTEL

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `AGENTICORE_OTEL_ENABLED` | `otel.enabled` | `true` | Enable OTEL telemetry |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `otel.endpoint` | `http://otel-collector:4317` | OTLP collector endpoint |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `otel.protocol` | `grpc` | OTLP protocol (`grpc` or `http`) |
| `AGENTICORE_OTEL_LOG_PROMPTS` | `otel.log_prompts` | `false` | Log user prompts in telemetry |
| `AGENTICORE_OTEL_LOG_TOOL_DETAILS` | `otel.log_tool_details` | `true` | Log tool call details |

### Anthropic

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `CLAUDE_CODE_OAUTH_TOKEN` | (env only) | (none) | Long-lived OAuth token for direct Anthropic auth. When set, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` are removed from the job env. |
| `ANTHROPIC_AUTH_TOKEN` | (env only) | (none) | Static API key (fallback when `CLAUDE_CODE_OAUTH_TOKEN` is not set) |
| `ANTHROPIC_BASE_URL` | (env only) | (none) | Custom API endpoint (e.g. LiteLLM proxy) |

### GitHub

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `GITHUB_TOKEN` | `github.token` | (none) | GitHub PAT for auto-PR + private repo access |
| `GITHUB_APP_ID` | `github.app_id` | (none) | GitHub App numeric ID |
| `GITHUB_APP_INSTALLATION_ID` | `github.app_installation_id` | (none) | GitHub App installation ID |
| `GITHUB_APP_PRIVATE_KEY_PATH` | (env only) | (none) | Path to GitHub App PEM key file |
| `GITHUB_APP_PRIVATE_KEY` | (env only) | (none) | Raw PEM text (K8s `--from-file` secrets) |
| `GITHUB_APP_PRIVATE_KEY_BASE64` | (env only) | (none) | Base64-encoded PEM key |

### Langfuse

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `LANGFUSE_HOST` | `langfuse.host` | `https://cloud.langfuse.com` | Langfuse API host |
| `LANGFUSE_PUBLIC_KEY` | `langfuse.public_key` | (none) | Enables Langfuse SDK tracing |
| `LANGFUSE_SECRET_KEY` | `langfuse.secret_key` | (none) | Langfuse SDK authentication |
| `LANGFUSE_BASIC_AUTH` | (env only) | (none) | Base64(`public_key:secret_key`) for OTEL collector |

### Agentihooks

**agentihooks is a PyPI dependency of agenticore.** `pip install agenticore`
pulls it transitively, so no configuration is required for the default
install path. The variables below are override escape hatches for
development against a live checkout or a non-PyPI fork/branch.

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `AGENTICORE_SHARED_FS_ROOT` | `repos.shared_fs_root` | (empty) | PVC root for state — `$HOME`, `.claude/`, `job-state/`. `/shared` in k8s, `$HOME` locally. |
| `AGENTICORE_CLONE_ROOT` | `repos.clone_root` | (empty) | Clone parent — `/app/clones` (emptyDir, per-pod ephemeral) in k8s. Falls back to `SHARED_FS_ROOT` when unset. |
| `AGENTICORE_AGENTIHOOKS_URL` | `agentihooks_url` | (empty) | Clone ONCE to `<CLONE_ROOT>/<dir-from-url>` + `uv pip install -e`. Default install is from PyPI. |
| `AGENTICORE_AGENTIHOOKS_BRANCH` | `agentihooks_branch` | (empty) | Git ref checked out when URL is set. |
| `AGENTICORE_AGENTIHOOKS_BUNDLE_URL` | `agentihooks_bundle_url` | (empty) | Git URL for the bundle content repo (optional). Passed as `--bundle` to `agentihooks init`. |
| `AGENTIHOOKS_PROFILE` | (env only) | `coding` | Profile name passed to `agentihooks init --profile`. |

### Agentihub

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `AGENTICORE_AGENTIHUB_URL` | `agentihub_url` | (empty) | Git URL for the agentihub repo (required for agent mode). Clone lands at `<CLONE_ROOT>/<dir-from-url>`. |
| `AGENTIHUB_AGENT` | (env only) | (empty) | Agent name to load (matches `agents/{name}/` directory). |

## Repository Sync

agentihooks itself is a pip dependency — only cloned when
`AGENTICORE_AGENTIHOOKS_URL` is set (clone + editable install over PyPI).
The bundle is an optional addon. agentihub is required in agent mode. All
three repos clone ONCE at startup; no background watchers.

### Which repos to configure

| Deployment | Repos needed | Key variables |
|------------|-------------|---------------|
| **Standard mode** (job orchestrator) | bundle (optional) | `AGENTICORE_AGENTIHOOKS_BUNDLE_URL` |
| **Agent mode** (purpose-built container) | bundle (optional) + hub (required) | `AGENTICORE_AGENTIHOOKS_BUNDLE_URL`, `AGENTICORE_AGENTIHUB_URL`, `AGENTIHUB_AGENT` |
| **Local dev** (bind-mounted checkouts) | mount at `<SHARED_FS_ROOT>/<dir-from-url>` | `AGENTICORE_SHARED_FS_ROOT`, plus matching URL env vars |

### Startup flow

```
Server start
  ├─ AGENTICORE_AGENTIHOOKS_URL set?         (override → editable install)
  │   ├─ destination dir already populated?  → trust it, no clone
  │   └─ otherwise                           → git clone + uv pip install -e
  ├─ AGENTICORE_AGENTIHOOKS_BUNDLE_URL set?  (optional addon)
  │   └─ clone → pass to agentihooks init --bundle
  └─ AGENTICORE_AGENTIHUB_URL set?           (required for agent mode)
      └─ clone (once)
          └─ agent_mode/initializer.py points package_dir at agents/{name}/package/
```

### Reload model

No background polling. To pick up upstream changes, trigger a sync on
demand or restart the pod. In agent mode, `render_mcp_whitelist` re-runs
`agentihooks init --repo <pkg>` before every completion, so per-agent
settings always reflect the on-disk bundle at request time.

### On-demand sync

**REST API:**

```bash
# Sync all repos
curl -X POST http://localhost:8200/admin/sync

# Sync a specific repo
curl -X POST "http://localhost:8200/admin/sync?target=agentihub"
```

Valid targets: `all` (default), `agentihooks`, `bundle`, `agentihub`.

**CLI (from inside the container):**

```bash
# Sync all repos
agenticore hooks sync

# Sync a specific repo
agenticore hooks sync --target bundle
```

### PATH vs URL

Setting a `*_PATH` variable tells agenticore to use that directory as-is —
no cloning, no git operations. This is useful for local development where
you have the repos already checked out. When both `*_PATH` and `*_URL` are
set, `*_PATH` takes precedence (agentihooks only; agentihub always clones
when `*_URL` is set).

## File Paths

### Local / Docker mode

| Path | Purpose |
|------|---------|
| `~/.agenticore/config.yml` | Main configuration file |
| `~/.agenticore/jobs/{id}.json` | Job data (file fallback) |
| `~/.agenticore/profiles/*/` | User profiles |
| `~/agenticore-repos/` | Default cloned repos root |
| `~/agenticore-repos/{hash}/.lock` | Per-repo flock file |
| `~/agenticore-repos/{hash}/repo/` | Cloned repository |
| `{cfg.runtime.agentihooks_dir}/profiles/*/` | Agentihooks profiles (when URL override is set) |

### Kubernetes / shared FS mode (`AGENTICORE_SHARED_FS_ROOT=/shared`)

| Path | Purpose |
|------|---------|
| `/shared/repos/{hash}/repo/` | Cloned repos on shared volume |
| `/shared/jobs/{job-id}/` | Per-job merge dir (extends profiles) / no-repo CWD |
| `/shared/job-state/{id}.json` | Job data files (`AGENTICORE_JOBS_DIR=/shared/job-state`) |
| `/shared/agentihooks/` | Cloned agentihooks repo (only when `AGENTICORE_AGENTIHOOKS_URL` override is set — default uses PyPI, no clone) |
| `/shared/agentihooks-bundle/` | Cloned bundle repo (when `AGENTICORE_AGENTIHOOKS_BUNDLE_URL` set) |
| `/shared/agentihub/` | Cloned agentihub repo (when `AGENTICORE_AGENTIHUB_URL` set) |
| `/app/worktrees/{job-id}/` | Bespoke worktrees (emptyDir, local disk via `AGENTICORE_WORKTREE_ROOT`) |
