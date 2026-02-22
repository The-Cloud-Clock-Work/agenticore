# OTEL Observability Pipeline

Agenticore uses OpenTelemetry to collect traces, metrics, and logs from Claude Code
subprocess executions. The pipeline flows from Claude Code through an OTEL Collector
to PostgreSQL, where data can be queried by Grafana or Langfuse.

## Pipeline Architecture

```
+-------------------+     +-------------------+     +-------------------+
|  Claude Code      |     |  OTEL Collector   |     |  PostgreSQL       |
|  (subprocess)     |---->|  :4317 (gRPC)     |---->|  :5432            |
|                   |     |  :4318 (HTTP)     |     |  agenticore db    |
|  OTEL env vars    |     |  batch processor  |     |                   |
|  injected by      |     |                   |     |  Queryable with   |
|  runner.py        |     |                   |     |  standard SQL     |
+-------------------+     +-------------------+     +-------------------+
                                                            |
                                                            v
                                                    +-------+-------+
                                                    |  Grafana /    |
                                                    |  Langfuse     |
                                                    +---------------+
```

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
```

- **postgresql** — Writes telemetry to PostgreSQL tables
- **debug** — Logs telemetry to collector stdout (for development)

### Service Pipelines

```yaml
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
```

All three signal types (traces, metrics, logs) use the same receive-process-export
pipeline. Switch exporters from `debug` to `postgresql` for production.

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

## Connecting Langfuse

Langfuse can consume OTEL data from the same PostgreSQL instance or be configured
as an additional exporter in the collector config.

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
