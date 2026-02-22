# Kubernetes Deployment

Agenticore includes a Helm chart for Kubernetes deployment with configurable
Redis, PostgreSQL, and OTEL Collector sidecars.

## Quick Install

```bash
helm install agenticore ./helm/agenticore \
  --set github.token=$GITHUB_TOKEN \
  --set server.apiKeys="your-secret-key"
```

## Chart Structure

```
helm/agenticore/
├── Chart.yaml              # Chart metadata (v0.1.0)
├── values.yaml             # Default configuration values
└── templates/
    ├── _helpers.tpl         # Template helpers (labels, fullname)
    ├── configmap.yaml       # Non-secret env vars
    ├── secret.yaml          # API keys, tokens
    ├── deployment.yaml      # Agenticore pod spec
    ├── service.yaml         # ClusterIP service
    ├── pvc.yaml             # PersistentVolumeClaims (repos + jobs)
    └── redis.yaml           # Redis deployment + service
```

## Values Reference

### Image

| Key | Default | Description |
|-----|---------|-------------|
| `image.repository` | `agenticore` | Container image |
| `image.tag` | `latest` | Image tag |
| `image.pullPolicy` | `IfNotPresent` | Pull policy |
| `replicaCount` | `1` | Number of replicas |

### Server

| Key | Default | Description |
|-----|---------|-------------|
| `server.host` | `0.0.0.0` | Bind address |
| `server.port` | `8200` | Listen port |
| `server.transport` | `sse` | Transport mode |
| `server.apiKeys` | `""` | Comma-separated API keys |
| `service.type` | `ClusterIP` | Kubernetes service type |
| `service.port` | `8200` | Service port |

### Claude

| Key | Default | Description |
|-----|---------|-------------|
| `claude.binary` | `claude` | Claude CLI binary |
| `claude.timeout` | `3600` | Max seconds per job |
| `claude.defaultProfile` | `code` | Default profile |
| `claude.configDir` | `""` | Custom CLAUDE_CONFIG_DIR |

### Redis

| Key | Default | Description |
|-----|---------|-------------|
| `redis.url` | `redis://redis:6379/0` | Redis connection URL |
| `redis.keyPrefix` | `agenticore` | Key namespace |
| `redis-standalone.enabled` | `true` | Deploy Redis subchart |
| `redis-standalone.image` | `redis:7-alpine` | Redis image |
| `redis-standalone.port` | `6379` | Redis port |

### OTEL

| Key | Default | Description |
|-----|---------|-------------|
| `otel.enabled` | `true` | Enable telemetry |
| `otel.endpoint` | `http://otel-collector:4317` | Collector endpoint |
| `otel.protocol` | `grpc` | OTLP protocol |
| `otel.logPrompts` | `false` | Log prompts |
| `otel.logToolDetails` | `true` | Log tool details |
| `otelCollector.enabled` | `true` | Deploy OTEL collector |
| `otelCollector.image` | `otel/opentelemetry-collector-contrib:latest` | Collector image |

### Repos

| Key | Default | Description |
|-----|---------|-------------|
| `repos.root` | `/data/repos` | Repos mount path |
| `repos.maxParallelJobs` | `3` | Max concurrent jobs |
| `repos.jobTtlSeconds` | `86400` | Job TTL |

### GitHub

| Key | Default | Description |
|-----|---------|-------------|
| `github.token` | `""` | GitHub token for auto-PR |

### PostgreSQL

| Key | Default | Description |
|-----|---------|-------------|
| `postgres.enabled` | `true` | Deploy PostgreSQL |
| `postgres.image` | `postgres:16-alpine` | PostgreSQL image |
| `postgres.database` | `agenticore` | Database name |
| `postgres.user` | `agenticore` | Database user |
| `postgres.password` | `agenticore` | Database password |

### Resources

| Key | Default | Description |
|-----|---------|-------------|
| `resources.requests.cpu` | `100m` | CPU request |
| `resources.requests.memory` | `256Mi` | Memory request |
| `resources.limits.cpu` | `2` | CPU limit |
| `resources.limits.memory` | `2Gi` | Memory limit |

### Scheduling

| Key | Default | Description |
|-----|---------|-------------|
| `nodeSelector` | `{}` | Node selector |
| `tolerations` | `[]` | Tolerations |
| `affinity` | `{}` | Affinity rules |

## Persistence

Two PersistentVolumeClaims are created when enabled:

| PVC | Default Size | Mount Path | Purpose |
|-----|-------------|------------|---------|
| `{release}-agenticore-repos` | `10Gi` | `/data/repos` | Cloned repositories |
| `{release}-agenticore-jobs` | `1Gi` | `/root/.agenticore/jobs` | Job data files |

```yaml
persistence:
  repos:
    enabled: true
    size: 10Gi
    storageClass: ""    # Use cluster default
  jobs:
    enabled: true
    size: 1Gi
    storageClass: ""
```

Both use `ReadWriteOnce` access mode.

## Redis Subchart

When `redis-standalone.enabled: true`, the chart deploys a single-replica Redis
instance with:

- Deployment: `{release}-agenticore-redis`
- Service: `redis` (ClusterIP)
- Readiness probe: `redis-cli ping`

To use an external Redis, disable the subchart and set `redis.url`:

```yaml
redis-standalone:
  enabled: false
redis:
  url: "redis://my-external-redis:6379/0"
```

## Health Probes

The Agenticore deployment includes:

| Probe | Endpoint | Timing |
|-------|----------|--------|
| Liveness | `GET /health` | Initial: 10s, Period: 30s |
| Readiness | `GET /health` | Initial: 5s, Period: 10s |

## Secrets Management

Sensitive values are stored in a Kubernetes Secret:

| Key | Source | Description |
|-----|--------|-------------|
| `AGENTICORE_API_KEYS` | `server.apiKeys` | API authentication keys |
| `GITHUB_TOKEN` | `github.token` | GitHub token for auto-PR |
| `AGENTICORE_CLAUDE_CONFIG_DIR` | `claude.configDir` | Claude config directory |

All values are base64-encoded. The pod loads them via `secretRef` in `envFrom`.

Non-secret configuration is stored in a ConfigMap with the same `envFrom`
pattern, containing all the env vars listed in the
[Configuration Reference](../reference/configuration.md).

## Example: Production Override

```bash
helm install agenticore ./helm/agenticore \
  --set image.repository=ghcr.io/my-org/agenticore \
  --set image.tag=v0.1.0 \
  --set github.token=$GITHUB_TOKEN \
  --set server.apiKeys="key1,key2" \
  --set redis-standalone.enabled=false \
  --set redis.url="redis://production-redis:6379/0" \
  --set persistence.repos.size=50Gi \
  --set persistence.repos.storageClass=gp3 \
  --set resources.limits.memory=4Gi
```
