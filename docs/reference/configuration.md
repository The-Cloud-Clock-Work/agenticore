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

Environment variables always win. If a YAML key and an env var both set the same
value, the env var takes effect.

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
| `AGENTICORE_CLAUDE_CONFIG_DIR` | `claude.config_dir` | (none) | Custom `CLAUDE_CONFIG_DIR` directory |

### Repos

| Variable | YAML Key | Default | Description |
|----------|----------|---------|-------------|
| `AGENTICORE_REPOS_ROOT` | `repos.root` | `~/agenticore-repos` | Root directory for cloned repos |
| `AGENTICORE_MAX_PARALLEL_JOBS` | `repos.max_parallel_jobs` | `3` | Max concurrent jobs |
| `AGENTICORE_JOB_TTL` | `repos.job_ttl_seconds` | `86400` | Job TTL in seconds (24h default) |

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

| Path | Purpose |
|------|---------|
| `~/.agenticore/config.yml` | Main configuration file |
| `~/.agenticore/jobs/{id}.json` | Job data (file fallback) |
| `~/.agenticore/profiles/*.yml` | Custom user profiles |
| `~/agenticore-repos/` | Default cloned repos root |
| `~/agenticore-repos/{hash}/.lock` | Per-repo flock file |
| `~/agenticore-repos/{hash}/repo/` | Cloned repository |
| `defaults/profiles/*/` | Bundled default profiles (directory-based) |
| `{AGENTICORE_AGENTIHOOKS_PATH}/profiles/*/` | External agentihooks profiles |
