---
title: CLI Commands
nav_order: 1
parent: Reference
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
agenticore update --source git+https://github.com/The-Cloud-Clockwork/agenticore.git
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

Interactive TUI for discovering and managing agents. **Local agent packages are always
discovered; Kubernetes is an opt-in backend.**

```bash
agenticore agents                          # local agents only — no kubectl, ever
agenticore agents --k8s                    # + K8s pods (all namespaces)
agenticore agents --namespace anton-prod   # + K8s pods, scoped (implies --k8s)
agenticore agents --agentihub-dir /path    # custom agentihub location
```

### Kubernetes is opt-in

By default the `agents` command never shells out to `kubectl` and renders no K8s chrome,
so it works on a laptop with no cluster (and leaves room for other backends — Fargate,
ECS — to be added later). Enable K8s in any of three ways:

| Where | How |
|-------|-----|
| CLI | `--k8s` (or `--namespace ns`, which implies it). `--no-k8s` forces it off. |
| Env | `AGENTICORE_K8S_ENABLED=true`, optional `AGENTICORE_K8S_NAMESPACES=ns-a,ns-b` |
| Config | `~/.agenticore/state.json` → `{"k8s": {"enabled": true, "namespaces": ["anton-prod"]}}` — written by the `k` key in the TUI |

**Precedence:** CLI flag > env > `state.json` > off.

Namespaces named *on the command line* imply `--k8s` — it is an unambiguous request for the
backend. Namespaces coming from the env or `state.json` do **not**: ambient config must never
resurrect Kubernetes for an operator who did not ask for it. An explicit `--no-k8s` always wins.

With no namespaces configured, discovery is all-namespaces. Otherwise each namespace is queried
separately (`kubectl` honours only the last `-n`).

### Two agent types

- **LOCAL** (green tag) — Claude Code agent packages from `agentihub/agents/*/package/`, found via
  `--agentihub-dir`, `AGENTIHUB_DIR`, `state.json`, or an ecosystem sibling-path walk. Description
  and capabilities are read from each package's `command.yml`.
- **K8S** (yellow tag, opt-in) — pods discovered via `kubectl get pods`. Any pod with an
  `AGENTICORE_TRANSPORT` env var. Pods with `AGENT_MODE=true` are agents; others are orchestrators.

**Actions — LOCAL agents:**

| Action | What it does |
|--------|--------------|
| Enter Agent Dir | `cd` to the package dir and drop into a shell |
| Open Chat | `cd` to the package dir + launch `claude` (flags come from the agentihooks profile) |
| Open in VS Code | `code <package_path>` |
| View CLAUDE.md | Display `package/CLAUDE.md` |

**Actions — K8S pods:**

| Action | Agent Mode | Standard | What it does |
|--------|-----------|----------|--------------|
| Remote Chat | Yes | — | `POST /completions` with interactive message input |
| Live Chat | Yes (with local match) | — | `kubectl exec -it -- bash -ic anton` into the container |
| Submit job | — | Yes | `POST /jobs` with task + repo URL |
| Sync repos | Yes | Yes | `agenticore hooks sync` inside the pod |
| Exec shell | Yes | Yes | `kubectl exec -it` into bash |
| Logs | Yes | Yes | `kubectl logs -f` |
| Health | Yes | Yes | `GET /health` |

Live Chat appears when a K8S pod's `AGENTIHUB_AGENT` name matches a local agent package.

**Keyboard (interactive):**

| Key | Action |
|-----|--------|
| `1-N` | Select an agent |
| `/word` | Filter by name, description, or capability |
| `/` | Clear filter |
| `c` | Set the agentihub directory (persisted to `state.json`) |
| `k` | Toggle the K8s backend + namespace scope (persisted to `state.json`) |
| `r` | Refresh |
| `q` | Quit |

### Headless mode (`--headless`)

For AI agents and scripts. All output is JSON to stdout, errors to stderr.

```bash
# List all agents — local only by default
agenticore agents --headless list

# Include K8s pods
agenticore agents --headless list --k8s
agenticore agents --headless list --namespace anton-prod

# Chat with an agent-mode pod
agenticore agents --headless chat --k8s --pod publishing-agent-0 --message "analyze the auth module"

# Submit a job to an orchestrator pod
agenticore agents --headless job --k8s --pod agenticore-0 --task "fix the bug" --repo https://github.com/org/repo

# Sync repos on a pod
agenticore agents --headless sync --k8s --pod agenticore-0

# Health check a pod
agenticore agents --headless health --k8s --pod publishing-agent-0

# Get local agent info (never needs K8s)
agenticore agents --headless local --agent finops
```

`list` reports the backend state so a caller can tell "no pods" from "K8s is off":

```json
{
  "k8s": { "enabled": false, "namespaces": [] },
  "agentihub": "/home/you/dev/tcc-ecosystem/agentihub",
  "pods": [],
  "local_agents": [
    { "name": "finops", "package_path": "…/agents/finops/package",
      "description": "AWS cost analyst…", "capabilities": ["cost-analysis", "…"] }
  ]
}
```

Pod actions (`chat`, `job`, `sync`, `health`) exit `2` with an actionable error when K8s is off.

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--headless` | flag | yes | Enable headless mode |
| `--k8s` / `--no-k8s` | flag | no | Enable / force-disable the K8s backend (default: off) |
| `--namespace` | string | no | Comma-separated K8s namespace scope (implies `--k8s`; empty = all-namespaces) |
| `--pod` | string | for K8S actions | Target pod name |
| `--agent` | string | for local | Local agent name |
| `--agentihub-dir` | string | no | Override agentihub directory path |
| `--message` | string | for chat | Message to send |
| `--task` | string | for job | Task description |
| `--repo` | string | for job | Repository URL |
| `--no-wait` | flag | no | Don't wait for chat response |

Exit codes: `0` = success, `1` = failure, `2` = invalid input.

## hooks sync

Clone or update companion repos (agentihooks, bundle, agentihub) and rebuild profiles.
Runs locally — does not require a running server.

```bash
# Sync all repos (agentihooks target is a no-op unless URL override is set)
agenticore hooks sync

# Sync a specific content repo (the common case)
agenticore hooks sync --target bundle
agenticore hooks sync --target agentihub

# Re-pull the agentihooks clone (only meaningful when AGENTICORE_AGENTIHOOKS_URL
# is set; otherwise agentihooks comes from PyPI and does not re-sync — restart
# the pod for a new pip-resolved version)
agenticore hooks sync --target agentihooks --url https://github.com/org/agentihooks
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--target` | choice | `all` | Which repo: `all`, `agentihooks` (URL-override only), `bundle`, `agentihub` |
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
