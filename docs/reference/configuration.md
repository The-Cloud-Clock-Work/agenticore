---
title: Configuration
nav_order: 2
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
  default_profile: code
  config_dir: ""

repos:
  root: ~/agenticore-repos
  max_parallel_jobs: 3
  job_ttl_seconds: 86400
  # Kubernetes / shared FS (optional):
  shared_fs_root: ""
  jobs_dir: ""
  pod_name: ""

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

agentihooks_path: ""
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
| `AGENTICORE_DEFAULT_PROFILE` | `claude.default_profile` | `code` | Default execution profile |
| `AGENTICORE_CLAUDE_CONFIG_DIR` | `claude.config_dir` | (none) | Static `CLAUDE_CONFIG_DIR` (overridden per-job in K8s mode) |

### Repos

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `AGENTICORE_REPOS_ROOT` | `repos.root` | `~/agenticore-repos` | Root directory for cloned repos |
| `AGENTICORE_MAX_PARALLEL_JOBS` | `repos.max_parallel_jobs` | `3` | Max concurrent jobs |
| `AGENTICORE_JOB_TTL` | `repos.job_ttl_seconds` | `86400` | Job TTL in seconds (24h default) |
| `AGENTICORE_SHARED_FS_ROOT` | `repos.shared_fs_root` | (none) | Shared RWX filesystem root (Kubernetes). When set, enables K8s mode: profiles are materialised to `/shared/jobs/{job-id}/` instead of the repo working dir. |
| `AGENTICORE_JOBS_DIR` | `repos.jobs_dir` | `~/.agenticore/jobs` | Override job JSON file directory. In K8s use `/shared/job-state`. |
| `AGENTICORE_POD_NAME` | `repos.pod_name` | `hostname` | Pod identity recorded on each job. Set from K8s Downward API (`metadata.name`). |

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

### GitHub

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `GITHUB_TOKEN` | `github.token` | (none) | GitHub token for auto-PR creation |

### Langfuse

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `LANGFUSE_HOST` | `langfuse.host` | `https://cloud.langfuse.com` | Langfuse API host |
| `LANGFUSE_PUBLIC_KEY` | `langfuse.public_key` | (none) | Enables Langfuse SDK tracing |
| `LANGFUSE_SECRET_KEY` | `langfuse.secret_key` | (none) | Langfuse SDK authentication |
| `LANGFUSE_BASIC_AUTH` | (env only) | (none) | Base64(`public_key:secret_key`) for OTEL collector |

### Agentihooks

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `AGENTICORE_AGENTIHOOKS_PATH` | `agentihooks_path` | (none) | Path to cloned agentihooks repo. Adds `{path}/profiles/` as a profile search directory |

## File Paths

### Local / Docker mode

| Path | Purpose |
|------|---------|
| `~/.agenticore/config.yml` | Main configuration file |
| `~/.agenticore/jobs/{id}.json` | Job data (file fallback) |
| `~/.agenticore/profiles/*/` | Custom user profiles |
| `~/agenticore-repos/` | Default cloned repos root |
| `~/agenticore-repos/{hash}/.lock` | Per-repo flock file |
| `~/agenticore-repos/{hash}/repo/` | Cloned repository |
| `defaults/profiles/*/` | Bundled default profiles |
| `{AGENTICORE_AGENTIHOOKS_PATH}/profiles/*/` | External agentihooks profiles |

### Kubernetes / shared FS mode (`AGENTICORE_SHARED_FS_ROOT=/shared`)

| Path | Purpose |
|------|---------|
| `/shared/profiles/{name}/.claude/` | Bundled profile files (populated by `init-shared-fs`) |
| `/shared/repos/{hash}/repo/` | Cloned repos on shared volume |
| `/shared/jobs/{job-id}/` | Per-job `CLAUDE_CONFIG_DIR` (profile files for one job) |
| `/shared/job-state/{id}.json` | Job data files (`AGENTICORE_JOBS_DIR=/shared/job-state`) |
