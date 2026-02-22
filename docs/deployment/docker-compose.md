# Docker Compose Deployment

Agenticore ships a Docker Compose stack with four services: the Agenticore server,
Redis for job storage, PostgreSQL as the OTEL sink, and an OpenTelemetry Collector.

## Quick Start

```bash
docker compose up --build -d
```

The server is available at `http://localhost:8200`.

## Stack Overview

```
+-------------------+      +-------------------+
|   agenticore      |      |   otel-collector   |
|   :8200           +----->|   :4317 (gRPC)     |
|   (server)        |      |   :4318 (HTTP)     |
+--------+----------+      +--------+----------+
         |                           |
         v                           v
+--------+----------+      +--------+----------+
|   redis           |      |   postgres         |
|   :6379           |      |   :5432            |
|   (job store)     |      |   (OTEL sink)      |
+-------------------+      +-------------------+
```

## Services

| Service | Image | Port | Volume | Healthcheck |
|---------|-------|------|--------|-------------|
| `agenticore` | Built from `Dockerfile` | 8200 | `repos-data`, `jobs-data` | (none) |
| `redis` | `redis:7-alpine` | 6379 | `redis-data` | `redis-cli ping` |
| `postgres` | `postgres:16-alpine` | 5432 | `pg-data` | `pg_isready -U agenticore` |
| `otel-collector` | `otel/opentelemetry-collector-contrib:latest` | 4317, 4318 | config mount | (none) |

### Startup Order

```
postgres (healthy) --> otel-collector
redis (healthy)    --> agenticore
```

The `agenticore` service waits for Redis to be healthy. The `otel-collector`
waits for PostgreSQL to be healthy.

## Volumes

| Volume | Mount Point | Purpose |
|--------|-------------|---------|
| `repos-data` | `/root/agenticore-repos` | Cloned repository cache |
| `jobs-data` | `/root/.agenticore/jobs` | Job JSON files (fallback store) |
| `redis-data` | `/data` | Redis persistence |
| `pg-data` | `/var/lib/postgresql/data` | PostgreSQL data |

## Dockerfile Walkthrough

The `Dockerfile` builds the Agenticore image:

```
python:3.12-slim
    |
    +--> Install git, curl
    +--> Install gh CLI (for auto-PR)
    +--> Copy pyproject.toml, agenticore/, defaults/
    +--> pip install -e .
    +--> Create dirs: /app/logs, jobs, profiles, repos
    +--> Set env: TRANSPORT=sse, HOST=0.0.0.0, PORT=8200
    +--> EXPOSE 8200
    +--> CMD: python -m agenticore
```

## Environment Variables

The compose file passes these to the `agenticore` service:

| Variable | Value | Description |
|----------|-------|-------------|
| `AGENTICORE_TRANSPORT` | `sse` | HTTP transport |
| `AGENTICORE_HOST` | `0.0.0.0` | Bind all interfaces |
| `AGENTICORE_PORT` | `8200` | Listen port |
| `AGENTICORE_API_KEYS` | from `.env` | Auth keys (optional) |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection |
| `REDIS_KEY_PREFIX` | `agenticore` | Redis key namespace |
| `AGENTICORE_REPOS_ROOT` | `/root/agenticore-repos` | Repos volume |
| `AGENTICORE_DEFAULT_PROFILE` | from `.env` | Default profile |
| `AGENTICORE_CLAUDE_BINARY` | from `.env` | Claude binary path |
| `AGENTICORE_OTEL_ENABLED` | from `.env` | Enable OTEL |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4317` | Collector endpoint |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` | OTLP protocol |
| `GITHUB_TOKEN` | from `.env` | GitHub token for auto-PR |

Variables with `${VAR:-default}` syntax are sourced from a `.env` file at the
project root. The `.env` file must exist (even if empty).

## PostgreSQL Configuration

The OTEL sink PostgreSQL instance uses these defaults:

| Setting | Value |
|---------|-------|
| Database | `agenticore` |
| User | `agenticore` |
| Password | `agenticore` |

Override these for production deployments.

## Customization

### Change the exposed port

```bash
AGENTICORE_PORT=9000 docker compose up -d
```

### Add API key auth

Create a `.env` file:

```bash
AGENTICORE_API_KEYS=your-secret-key-1,your-secret-key-2
GITHUB_TOKEN=ghp_...
```

### Use an external Redis

Remove the `redis` service from the compose file and set `REDIS_URL` to your
external Redis instance.

## Troubleshooting

**Redis connection refused on startup:**
The `agenticore` service depends on Redis with `condition: service_healthy`.
If Redis takes too long to start, the health check retries (5 attempts, 5s
interval). Check `docker compose logs redis`.

**Missing `.env` file:**
Docker Compose expects a `.env` file for variable substitution. Create an empty
one if you don't need custom values:

```bash
touch .env
```

**Build errors with buildx:**
Ensure Docker buildx version is >= 0.17.0:

```bash
docker buildx version
```

**OTEL collector not receiving data:**
Verify the collector is running and the endpoint is reachable from the
agenticore container:

```bash
docker compose exec agenticore curl -s http://otel-collector:4317
```
