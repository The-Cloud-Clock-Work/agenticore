---
title: OTEL Pipeline
nav_order: 3
---

# OTEL Observability Pipeline

Agenticore uses OpenTelemetry to collect traces, metrics, and logs from Claude Code
subprocess executions. The pipeline flows from Claude Code through an OTEL Collector
to PostgreSQL, where data can be queried by Grafana or Langfuse.

## Pipeline Architecture

```
+-------------------+     +-------------------+     +-------------------+
|  Claude Code      |     |  OTEL Collector   |---->|  Langfuse         |
|  (subprocess)     |---->|  :4317 (gRPC)     |     |  (otlphttp)       |
|                   |     |  :4318 (HTTP)     |     +-------------------+
|  OTEL env vars    |     |  batch processor  |
|  injected by      |     |                   |---->+-------------------+
|  runner.py        |     |                   |     |  PostgreSQL       |
+-------------------+     +-------------------+     |  :5432            |
                                                    +-------------------+
+-------------------+
|  runner.py        |     (Langfuse SDK — direct)
|  telemetry.py     |---->  Job traces + session transcript spans
+-------------------+
```

There are **two paths** into Langfuse:

1. **OTEL Collector** exports Claude's native OTEL data (traces, metrics, logs) via `otlphttp/langfuse`
2. **Langfuse SDK** (in `telemetry.py`) creates job-level traces and ships Claude session transcripts as spans

## Collector Configuration

The collector config is at `otel-collector-config.yml` and mounted into the
collector container at `/etc/otelcol-contrib/config.yaml`.

### Receivers

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
```

Accepts OTLP data over both gRPC (port 4317) and HTTP (port 4318). The
Agenticore runner defaults to gRPC (`OTEL_EXPORTER_OTLP_PROTOCOL=grpc`).

### Processors

```yaml
processors:
  batch:
    timeout: 5s
    send_batch_size: 100
```

Batches telemetry data before exporting. Sends after 100 items or 5 seconds,
whichever comes first.

### Exporters

```yaml
exporters:
  postgresql:
    datasource: "host=postgres port=5432 user=agenticore password=agenticore dbname=agenticore sslmode=disable"

  debug:
    verbosity: basic

  otlphttp/langfuse:
    endpoint: ${env:LANGFUSE_HOST}/api/public/otel
    headers:
      Authorization: "Basic ${env:LANGFUSE_BASIC_AUTH}"
```

- **postgresql** — Writes telemetry to PostgreSQL tables
- **debug** — Logs telemetry to collector stdout (for development)
- **otlphttp/langfuse** — Exports to Langfuse's OTEL ingestion endpoint

The Langfuse exporter requires two environment variables on the collector container:
- `LANGFUSE_HOST` — e.g. `https://cloud.langfuse.com`
- `LANGFUSE_BASIC_AUTH` — Base64 encoding of `public_key:secret_key`

The collector service has `env_file: .env` so these are sourced from the project `.env` file.

### Service Pipelines

```yaml
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug, otlphttp/langfuse]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug, otlphttp/langfuse]
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug, otlphttp/langfuse]
```

All three signal types (traces, metrics, logs) are exported to both `debug` (stdout)
and `otlphttp/langfuse`. Add `postgresql` to the exporters list when you want SQL-queryable
storage.

## OTEL Configuration

These env vars control what the runner injects into the Claude subprocess:

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_OTEL_ENABLED` | `true` | Master switch |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4317` | Collector address |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` | Protocol (`grpc` or `http`) |
| `AGENTICORE_OTEL_LOG_PROMPTS` | `false` | Include user prompts in telemetry |
| `AGENTICORE_OTEL_LOG_TOOL_DETAILS` | `true` | Include tool call details |

When OTEL is enabled, the runner injects 7 environment variables into the Claude
subprocess. See [Job Execution](../architecture/job-execution.md) for the full
list.

## Connecting Grafana

Point Grafana at the PostgreSQL instance to query OTEL data:

```
Data source: PostgreSQL
Host: postgres:5432 (or localhost:5432 if port-forwarded)
Database: agenticore
User: agenticore
Password: agenticore
```

## Langfuse Integration

Langfuse receives data through two channels:

### 1. OTEL Collector (native Claude telemetry)

The `otlphttp/langfuse` exporter sends traces, metrics, and logs from Claude Code's
native OTEL output directly to Langfuse. This is configured in `otel-collector-config.yml`
and requires `LANGFUSE_HOST` + `LANGFUSE_BASIC_AUTH` in `.env`.

### 2. Langfuse SDK (job traces + transcripts)

The `telemetry.py` module uses the Langfuse Python SDK to:

- **Create a trace** for each job (`start_job_trace`) with task, profile, and repo metadata
- **Ship the Claude session transcript** as individual spans (`ship_transcript`) — each user/assistant turn becomes a span on the trace
- **Finalize the trace** (`end_job_trace`) with exit code, status, PR URL, and timing

This requires `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` in `.env`. The SDK is
non-fatal: if credentials are missing or Langfuse is unreachable, the runner continues
normally.

### Environment variables

| Variable | Used by | Description |
|----------|---------|-------------|
| `LANGFUSE_HOST` | Collector + SDK | Langfuse API host |
| `LANGFUSE_PUBLIC_KEY` | SDK | Enables SDK tracing |
| `LANGFUSE_SECRET_KEY` | SDK | SDK authentication |
| `LANGFUSE_BASIC_AUTH` | Collector | Base64(`public_key:secret_key`) for OTLP auth |

## Disabling OTEL

Set `AGENTICORE_OTEL_ENABLED=false` to disable telemetry entirely. When disabled,
no OTEL environment variables are injected into the Claude subprocess, and Claude
Code runs without telemetry emission.

```bash
# Via environment
AGENTICORE_OTEL_ENABLED=false agenticore run

# Via YAML config
otel:
  enabled: false
```

## Troubleshooting

**No data in PostgreSQL:**
1. Check the collector is running: `docker compose logs otel-collector`
2. Verify the PostgreSQL exporter is enabled in the pipeline (not `debug`)
3. Confirm the datasource connection string matches PostgreSQL credentials

**Claude subprocess not emitting telemetry:**
1. Verify `AGENTICORE_OTEL_ENABLED` is `true`
2. Check that the OTEL endpoint is reachable from the agenticore container
3. Look for OTEL errors in Claude's stderr output (visible in job error field)

**High collector memory usage:**
Reduce batch size or timeout in the processor config:

```yaml
processors:
  batch:
    timeout: 2s
    send_batch_size: 50
```
