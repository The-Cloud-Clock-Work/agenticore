---
title: CLI Commands
nav_order: 1
---

# CLI Reference

Agenticore provides a CLI for server management, job submission, and status queries.
Job-related commands communicate with a running server via REST API.

```
agenticore <command> [options]
```

## Summary

| Command | Args | Key Flags | Server Required |
|---------|------|-----------|-----------------|
| `serve` | | `--port`, `--host` | No (starts it) |
| `run` | `<task>` | `--repo`, `--profile`, `--wait` | Yes |
| `jobs` | | `--limit`, `--status` | Yes |
| `job` | `<job_id>` | `--json` | Yes |
| `cancel` | `<job_id>` | | Yes |
| `profiles` | | | Yes |
| `status` | | | Yes |
| `init-shared-fs` | | `--shared-root` | No |
| `drain` | | `--timeout` | No |
| `update` | | `--source` | No |
| `version` | | | No |
| `agent` | | `--build`, `--run`, `--enter`, `--stop`, `--logs`, `--list`, `--compose-*` | No |
| `push` | | `--main`, `--all`, `--tag`, `--build-only`, `--push-only`, `--no-cache` | No |
| `agents` | | | No (needs kubectl) |
| `hooks sync` | | `--target`, `--url` | No |

## serve

Start the Agenticore server.

```bash
agenticore serve [--port PORT] [--host HOST]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--port` | int | 8200 | Server port |
| `--host` | str | 127.0.0.1 | Bind address |

Transport mode is controlled by `AGENTICORE_TRANSPORT` (default: `sse`).

```bash
agenticore serve
agenticore serve --port 9000 --host 0.0.0.0
```

## run

Submit a task for Claude Code execution.

```bash
agenticore run <task> [--repo URL] [--profile NAME] [--base-ref REF] [--wait] [--session-id ID]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--repo` | `-r` | str | (none) | GitHub repo URL to clone |
| `--profile` | `-p` | str | (auto) | Execution profile name |
| `--base-ref` | | str | `main` | Base branch for PR |
| `--wait` | `-w` | flag | false | Wait for job completion |
| `--session-id` | | str | (none) | Resume a Claude session |

```bash
# Fire-and-forget
agenticore run "fix the auth bug" --repo https://github.com/org/repo

# Wait for result
agenticore run "add unit tests" -r https://github.com/org/repo -w

# Use specific profile
agenticore run "review this PR" -r https://github.com/org/repo -p review

# Resume session
agenticore run "continue the refactor" --session-id abc123
```

## jobs

List recent jobs.

```bash
agenticore jobs [--limit N] [--status STATUS]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--limit` | `-n` | int | 20 | Max jobs to return |
| `--status` | `-s` | str | (all) | Filter: `queued`, `running`, `succeeded`, `failed`, `cancelled` |

```bash
agenticore jobs
agenticore jobs -s running -n 50
```

Output is a table with columns: `ID`, `STATUS`, `PROFILE`, `TASK`.

## job

Get details for a specific job.

```bash
agenticore job <job_id> [--json]
```

| Flag | Type | Description |
|------|------|-------------|
| `--json` | flag | Output raw JSON instead of formatted text |

```bash
agenticore job a1b2c3d4-...
agenticore job a1b2c3d4-... --json
```

Displays: ID, status, profile, task, repo URL, exit code, PR URL, timestamps,
error message, and output (truncated to 2000 chars).

## cancel

Cancel a running or queued job.

```bash
agenticore cancel <job_id>
```

Sends SIGTERM to the Claude subprocess if the job is running.

## profiles

List available execution profiles.

```bash
agenticore profiles
```

```
  code         Autonomous coding worker
               model=sonnet max_turns=80 auto_pr=True
  review       Code review analyst
               model=haiku max_turns=20 auto_pr=False
```

## status

Check server health.

```bash
agenticore status
# Status:  ok
# Service: agenticore
```

## init-shared-fs

Initialise the shared filesystem layout for Kubernetes deployments.

```bash
agenticore init-shared-fs [--shared-root PATH]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--shared-root` | str | `$AGENTICORE_SHARED_FS_ROOT` | Shared FS root path |

Creates the directory layout and copies bundled profiles to the shared volume:

```
/shared/
├── profiles/    ← bundled profiles copied here
├── repos/       ← git clone root
├── jobs/        ← per-job merge directories (extends profiles, no-repo CWDs)
└── job-state/   ← job JSON files (AGENTICORE_JOBS_DIR)
```

Typically run once as a Kubernetes init Job before the StatefulSet starts. See
[Kubernetes Deployment](../deployment/kubernetes.md) for the manifest.

## drain

Drain the pod before shutdown. Called by the Kubernetes PreStop hook.

```bash
agenticore drain [--timeout SECONDS]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--timeout` | int | 300 | Max seconds to wait for running jobs |

Steps:
1. Marks this pod as draining in Redis (`agenticore:pod:{pod_name}:draining`)
2. Polls until all jobs with `pod_name == this pod` are no longer `running`
3. Removes the draining flag
4. Exits (Kubernetes then terminates the container)

The StatefulSet configures `terminationGracePeriodSeconds: 300` to give this time to finish.

## update

Self-update Agenticore to the latest version.

```bash
agenticore update [--source SOURCE]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--source` | str | `agenticore` | Install source (PyPI, git URL, or local path) |

```bash
agenticore update
agenticore update --source git+https://github.com/The-Cloud-Clock-Work/agenticore.git
agenticore update --source /path/to/agenticore
```

## version

Show the installed version.

```bash
agenticore version
# agenticore 0.1.5
```

## agent

Build, run, and manage the agenticore Docker container and the dev compose stack.

```bash
agenticore agent [flags]
```

### Container Management

| Flag | Short | Description |
|------|-------|-------------|
| `--build` | `-b` | Build the Docker image from the repo-root `Dockerfile` |
| `--run` | `-r` | Run the container in detached mode (port 8200) |
| `--enter` | `-e` | Shell into the running container |
| `--stop` | `-s` | Stop and remove the container |
| `--logs` | `-l` | Follow container logs |
| `--list` | | List all local Docker containers |

### Dev Compose Stack

| Flag | Description |
|------|-------------|
| `--compose-up` | `docker compose -f docker-compose.dev.yml up --build -d` |
| `--compose-down` | `docker compose -f docker-compose.dev.yml down` |
| `--compose-enter` | Shell into the running `agenticore` compose service |
| `--compose-logs` | Follow compose service logs |

The compose commands look for `docker-compose.dev.yml` starting from the current
directory and walking up (max 5 levels).

### `.env` lookup

Both `--run` and `--compose-*` flags load a `.env` file automatically:

1. Walk up from CWD looking for `.env`
2. Fall back to `$HOME/.env`

### Shell aliases

Run `bash automation/alias_setup.sh` to install `ac_*` shortcuts (e.g.
`ac_compose_up`, `ac_enter_agent`). See the
[Local Development](../../README.md#local-development) section in the README.

```bash
# Standalone container workflow
agenticore agent --build
agenticore agent --run
agenticore agent --enter
agenticore agent --logs
agenticore agent --stop

# Dev compose workflow
agenticore agent --compose-up
agenticore agent --compose-enter
agenticore agent --compose-logs
agenticore agent --compose-down
```

## push

Build and push the Docker image to a container registry. Requires the
`DOCKER_REGISTRY` environment variable.

```bash
agenticore push --main [--tag TAG] [--build-only] [--push-only] [--no-cache]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--main` | `-m` | flag | | Build and push the main image |
| `--all` | `-a` | flag | | Build and push all images (same as `--main`) |
| `--tag` | `-t` | str | `latest` | Image tag |
| `--build-only` | | flag | | Only build, skip the push step |
| `--push-only` | | flag | | Only push (assumes image was already built) |
| `--no-cache` | | flag | | Build without Docker cache |

```bash
# Build and push with default tag
DOCKER_REGISTRY=ghcr.io/org agenticore push --main

# Build only, custom tag
agenticore push --main --tag v1.2.3 --build-only

# Push a previously built image
agenticore push --main --push-only
```

## agents

Interactive TUI for discovering and managing agenticore pods in the current Kubernetes namespace.
Requires `kubectl` configured with cluster access.

```bash
agenticore agents
```

Discovers pods by scanning env vars — any pod with `AGENTICORE_TRANSPORT` set is an agenticore pod.
Pods with `AGENT_MODE=true` are classified as agents; others as orchestrators.

**Actions per pod:**

| Action | Agent Mode | Standard | What it does |
|--------|-----------|----------|--------------|
| Chat | Yes | — | `POST /completions` with interactive message input |
| Submit job | — | Yes | `POST /jobs` with task + repo URL |
| Sync repos | Yes | Yes | `agenticore hooks sync` inside the pod |
| Exec shell | Yes | Yes | `kubectl exec -it` into bash |
| Logs | Yes | Yes | `kubectl logs -f` |
| Health | Yes | Yes | `GET /health` |

**Keyboard (interactive):**

| Key | Action |
|-----|--------|
| `1-N` | Select a pod |
| `/word` | Filter by name |
| `/` | Clear filter |
| `r` | Refresh pod list |
| `q` | Quit |

### Headless mode (`--headless`)

For AI agents and scripts. All output is JSON to stdout, errors to stderr.

```bash
# List all agenticore pods
agenticore agents --headless list

# Chat with an agent-mode pod
agenticore agents --headless chat --pod publishing-agent-0 --message "analyze the auth module"

# Submit a job to an orchestrator pod
agenticore agents --headless job --pod agenticore-0 --task "fix the bug" --repo https://github.com/org/repo

# Sync repos on a pod
agenticore agents --headless sync --pod agenticore-0

# Health check a pod
agenticore agents --headless health --pod publishing-agent-0
```

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--headless` | flag | yes | Enable headless mode |
| `--pod` | string | for actions | Target pod name |
| `--message` | string | for chat | Message to send |
| `--task` | string | for job | Task description |
| `--repo` | string | for job | Repository URL |
| `--no-wait` | flag | no | Don't wait for chat response |

Exit codes: `0` = success, `1` = failure, `2` = invalid input.

## hooks sync

Clone or update companion repos (agentihooks, bundle, agentihub) and rebuild profiles.
Runs locally — does not require a running server.

```bash
# Sync all repos
agenticore hooks sync

# Sync a specific repo
agenticore hooks sync --target agentihub

# Override agentihooks URL
agenticore hooks sync --target agentihooks --url https://github.com/org/agentihooks
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--target` | choice | `all` | Which repo: `all`, `agentihooks`, `bundle`, `agentihub` |
| `--url` | str | env var | Git URL override (agentihooks target only) |

## Client Configuration

The CLI connects to the server using these environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_HOST` | `127.0.0.1` | Server host |
| `AGENTICORE_PORT` | `8200` | Server port |

```bash
AGENTICORE_HOST=10.0.0.5 AGENTICORE_PORT=9000 agenticore jobs
```
