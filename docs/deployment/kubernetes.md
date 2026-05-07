---
title: Kubernetes
nav_order: 2
parent: Deployment
---

# Kubernetes Deployment

Agenticore runs as a Kubernetes **StatefulSet** backed by a **shared RWX
PersistentVolumeClaim** so all pods share the same git repo cache and profile
files. Jobs are stored in Redis; any pod can serve any job request
(work-stealing). KEDA autoscales pods based on Redis queue depth.

The recommended deployment method is **Helm** from GHCR. Raw `k8s/` manifests
are available as a secondary option for non-Helm deployments.

## Architecture

```
Internet / Claude.ai ──► LoadBalancer :8200
                               │
              ┌────────────────▼─────────────────────────────┐
              │  Agenticore StatefulSet (agenticore-0..N)     │
              │                                               │
              │  agenticore-0  agenticore-1  agenticore-2    │
              │       └───────────────┴───────────────┘       │
              │              Work-stealing from Redis          │
              └──────────────────────────────────────────────┘
                        │                    │
               ┌────────▼──────┐  ┌─────────▼──────────┐
               │  Redis        │  │  Shared RWX PVC     │
               │  (jobs, queue)│  │  /shared/           │
               └───────────────┘  │  ├─ profiles/       │
                                  │  ├─ repos/           │
               KEDA ScaledObject  │  ├─ jobs/            │
               watches Redis ─────┘  └─ job-state/      │
                                     └────────────────────┘
```

### Storage layout

Agenticore uses two storage tiers: shared NFS for repos and state, local emptyDir for worktrees.

**Shared FS (NFS PVC, RWX):**

```
/shared/
├── repos/{hash}/repo/          ← git clone cache (shared across pods)
├── agentihooks/                ← only present when AGENTICORE_AGENTIHOOKS_URL override is set (default: agentihooks is a PyPI dep, no clone)
├── jobs/{job-id}/              ← per-job merge dir (extends profiles) / no-repo CWD
│   ├── .claude/
│   │   ├── settings.json
│   │   └── CLAUDE.md
│   └── .mcp.json
└── job-state/{id}.json         ← job file fallback (AGENTICORE_JOBS_DIR)
```

**Local disk (emptyDir, per-pod):**

```
/app/worktrees/                   ← AGENTICORE_WORKTREE_ROOT
├── {job_id_1}/                   ← bespoke worktree (ephemeral)
├── {job_id_2}/                   ← bespoke worktree (ephemeral)
└── ...
```

Worktrees are created on local disk (emptyDir) for I/O performance and to avoid NFS locking issues. They are ephemeral — pod restart clears them. Repos remain on shared FS so all pods share the clone cache.

### Memory sizing

Each Claude Code process uses ~320Mi memory. Size pod limits accordingly:

| Concurrent jobs | CPU request | Memory limit | Notes |
|----------------|-------------|-------------|-------|
| 2 | 1 CPU | 2Gi | Conservative default |
| 5 | 2 CPU | 3Gi | Light workloads |
| 10+ | 4 CPU | 4Gi | Verified: peak 1018m CPU / 789Mi at 10 jobs |

Set  to match your memory allocation.

**Agentihooks** ships as a pip dependency of agenticore ([PyPI](https://pypi.org/project/agentihooks/)),
so the CLI is available inside the container without any runtime configuration.
Set `AGENTICORE_AGENTIHOOKS_URL` to override with a one-shot clone + editable
install (bleeding-edge fork/branch); a bind-mount at `<SHARED_FS_ROOT>/<dir-from-url>`
is treated as a pre-existing clone (no fetch). There is no periodic re-sync —
restart the pod to pick up a newer PyPI release (bump the floor in agenticore's
`pyproject.toml`, or rebuild the image to pull the latest resolved version).
---

## Prerequisites

1. A Kubernetes cluster
2. A RWX-capable storage class (see table below)
3. Redis (in-cluster or external)
4. KEDA installed in the cluster (optional, for autoscaling)
5. The `agenticore-secrets` Kubernetes Secret

**RWX storage class options:**

| Environment | `storageClassName` |
|-------------|-------------------|
| On-prem NFS | `nfs-client` |
| AWS EFS | `efs-sc` |
| Azure Files | `azurefile-csi` |
| Ceph | `ceph-filesystem` |

---

## Install via Helm

The chart is published to GHCR and requires no additional registry configuration
for public clusters.

### 1. Create the Secret

```bash
kubectl create secret generic agenticore-secrets \
  --from-literal=redis-url="redis://redis:6379/0" \
  --from-literal=redis-address="redis:6379" \
  --from-literal=github-token="$GITHUB_TOKEN" \
  --from-literal=anthropic-api-key="$ANTHROPIC_AUTH_TOKEN"
```

### 2. Install the chart

```bash
helm install agenticore \
  oci://ghcr.io/the-cloud-clockwork/charts/agenticore \
  --version 0.1.5 \
  --set storage.className=nfs-client
```

### Agentihooks

To enable URL-based agentihooks cloning:

```yaml
agentihooks:
  url: "https://github.com/your-org/agentihooks"
```

Or via `--set`:

```bash
helm install agenticore \
  oci://ghcr.io/the-cloud-clockwork/charts/agenticore \
  --set agentihooks.url="https://github.com/your-org/agentihooks" \
  --set storage.className=nfs-client
```

### Common `--set` overrides

| Flag | Default | Description |
|------|---------|-------------|
| `storage.className` | `nfs-client` | RWX storage class (required) |
| `storage.size` | `100Gi` | PVC size |
| `replicas` | `2` | Static replica count (ignored when KEDA enabled) |
| `image.tag` | `latest` | Agenticore image tag |
| `image.repository` | `the-cloud-clockwork/agenticore` | Container image |
| `config.agentihooksProfile` | `coding` | Active profile (set by agentihooks) |
| `config.maxParallelJobs` | `3` | Max Claude subprocesses per pod |
| `keda.enabled` | `false` | Enable KEDA autoscaling |
| `keda.redisAddress` | `redis:6379` | Redis host:port for KEDA |
| `ingress.enabled` | `false` | Enable Ingress resource |
| `ingress.host` | `agenticore.example.com` | Ingress hostname |

Full reference: [`charts/agenticore/values.yaml`](../../charts/agenticore/values.yaml)

---

## Upgrade

```bash
helm upgrade agenticore \
  oci://ghcr.io/the-cloud-clockwork/charts/agenticore \
  --version 0.1.6
```

---

## KEDA Autoscaling

Enable with:

```bash
helm upgrade agenticore \
  oci://ghcr.io/the-cloud-clockwork/charts/agenticore \
  --set keda.enabled=true \
  --set keda.redisAddress=redis:6379
```

The chart deploys a `ScaledObject` that watches the `agenticore:queue` Redis
list and adds a replica for every 5 pending jobs, scaling from 1 to 10 pods.

Adjust via `--set keda.minReplicas=1 --set keda.maxReplicas=20 --set keda.listLength=10`.

---

## Key values reference

| Value | Default | Description |
|-------|---------|-------------|
| `nameOverride` | `""` | Override chart name |
| `fullnameOverride` | `""` | Override fully-qualified release name |
| `secretName` | `agenticore-secrets` | K8s Secret with credentials |
| `sharedFs.root` | `/shared` | Mount path for RWX PVC |
| `sharedFs.reposRoot` | `/shared/repos` | Git clone cache path |
| `sharedFs.jobsDir` | `/shared/job-state` | Job state fallback directory |
| `config.transport` | `sse` | Always `sse` in Kubernetes |
| `config.port` | `8200` | Server port |
| `config.jobTtl` | `86400` | Job TTL in Redis (seconds) |
| `initJob.enabled` | `true` | Run init job on install |
| `service.type` | `LoadBalancer` | Kubernetes Service type |
| `ingress.enabled` | `false` | Enable Ingress |

---

## Pod Identity and Graceful Shutdown

StatefulSet pods have stable DNS names:

```
agenticore-0.agenticore-headless.default.svc.cluster.local
agenticore-1.agenticore-headless.default.svc.cluster.local
```

`pod_name` is stored on every job record so you can trace which pod ran a job:

```bash
agenticore jobs --json | jq '.[].pod_name'
```

The StatefulSet sets `terminationGracePeriodSeconds: 300`. The PreStop hook
calls `agenticore drain --timeout 270` which:

1. Marks the pod as draining in Redis
2. Waits for all in-progress jobs to complete
3. Exits — Kubernetes then sends SIGTERM to the container

---

## Verification

```bash
# Check pods
kubectl get pods -l app=agenticore

# Check shared FS is mounted
kubectl exec agenticore-0 -- ls /shared/

# Port-forward and submit a test job
kubectl port-forward svc/agenticore 8200:8200 &
agenticore run "echo hello world" --wait

# Confirm job shows pod_name
agenticore job <id> --json | jq '.pod_name'

# Dry-run install to validate templates
helm install agenticore \
  oci://ghcr.io/the-cloud-clockwork/charts/agenticore \
  --version 0.1.5 \
  --dry-run --debug \
  --set storage.className=standard
```

---

## Migration from Docker Compose

Docker Compose mode remains fully supported. No code changes are needed to
continue using Docker Compose.

| Feature | Docker Compose | Kubernetes |
|---------|---------------|------------|
| Shared FS | No (local volume) | RWX PVC |
| Clone locking | `fcntl` flock | Redis `SET NX` |
| Profile materialization (simple) | `~/.claude/` (installed by agentihooks at startup) | Same |
| Profile materialization (extends) | Merged to `/tmp/agenticore-jobs/{id}/` | Merged to `/shared/jobs/{id}/` |
| Repo working dir | Never touched | Never touched |
| Pod identity | hostname | StatefulSet name (Downward API) |
| Scaling | Single container | KEDA ScaledObject |
| Drain | N/A | `agenticore drain` PreStop hook |

---

## Raw Manifests (non-Helm)

Raw Kubernetes manifests are in `k8s/` for deployments that cannot use Helm.
They require manual image tag substitution before applying.

| File | Resource | Purpose |
|------|----------|---------|
| `pvc-shared.yaml` | PersistentVolumeClaim | 100Gi RWX shared volume |
| `statefulset.yaml` | StatefulSet | Agenticore pods (2 replicas default) |
| `headless-service.yaml` | Service | Stable pod DNS (`agenticore-0.agenticore-headless`) |
| `service.yaml` | Service | LoadBalancer for external traffic |
| `keda-scaledobject.yaml` | ScaledObject + TriggerAuthentication | KEDA autoscaler |

```bash
# Replace image tag placeholder, then apply
sed -i 's|agenticore:latest|the-cloud-clockwork/agenticore:v0.1.5|g' k8s/statefulset.yaml
kubectl apply -f k8s/
```
