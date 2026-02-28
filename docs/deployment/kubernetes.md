---
title: Kubernetes
nav_order: 2
---

# Kubernetes Deployment

Agenticore runs as a Kubernetes **StatefulSet** backed by a **shared RWX
PersistentVolumeClaim** so all pods share the same git repo cache and profile
files. Jobs are stored in Redis; any pod can serve any job request
(work-stealing). KEDA autoscales pods based on Redis queue depth.

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

### Shared filesystem layout

```
/shared/
├── profiles/{name}/.claude/    ← static profile files (init-shared-fs)
├── repos/{hash}/repo/          ← git clone cache (shared across pods)
├── jobs/{job-id}/              ← per-job CLAUDE_CONFIG_DIR
│   ├── settings.json
│   ├── CLAUDE.md
│   └── .mcp.json
└── job-state/{id}.json         ← job file fallback (AGENTICORE_JOBS_DIR)
```

## Manifests

All manifests live in `k8s/`. Apply them with:

```bash
kubectl apply -f k8s/
```

| File | Resource | Purpose |
|------|----------|---------|
| `pvc-shared.yaml` | PersistentVolumeClaim | 100Gi RWX shared volume |
| `init-profiles.yaml` | Job | One-time: populate shared FS with profiles |
| `statefulset.yaml` | StatefulSet | Agenticore pods (2 replicas default) |
| `headless-service.yaml` | Service | Stable pod DNS (`agenticore-0.agenticore-headless`) |
| `service.yaml` | Service | LoadBalancer for external traffic |
| `keda-scaledobject.yaml` | ScaledObject + TriggerAuthentication | KEDA autoscaler |

## Prerequisites

1. A RWX storage class (NFS, AWS EFS, Azure Files, Ceph RBD FS)
2. Redis (in-cluster or external)
3. KEDA installed (optional, for autoscaling)
4. A Kubernetes Secret named `agenticore-secrets` with:

```bash
kubectl create secret generic agenticore-secrets \
  --from-literal=redis-url="redis://redis:6379/0" \
  --from-literal=redis-address="redis:6379" \
  --from-literal=github-token="$GITHUB_TOKEN" \
  --from-literal=anthropic-api-key="$ANTHROPIC_API_KEY"
```

## Deployment Steps

### 1. Create the shared PVC

Edit `k8s/pvc-shared.yaml` to set your storage class, then:

```bash
kubectl apply -f k8s/pvc-shared.yaml
```

Storage class options:

| Environment | `storageClassName` |
|-------------|-------------------|
| On-prem NFS | `nfs-client` |
| AWS EFS | `efs-sc` |
| Azure Files | `azurefile-csi` |
| Ceph | `ceph-filesystem` |

### 2. Initialise the shared FS (once)

```bash
kubectl apply -f k8s/init-profiles.yaml
kubectl wait --for=condition=complete job/agenticore-init-profiles
```

This runs `agenticore init-shared-fs` which creates the directory layout and
copies the bundled profile packages to `/shared/profiles/`.

### 3. Apply the StatefulSet and Services

```bash
kubectl apply -f k8s/headless-service.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/statefulset.yaml
```

### 4. (Optional) Enable KEDA autoscaling

```bash
kubectl apply -f k8s/keda-scaledobject.yaml
```

Scales from 1 to 10 pods based on `agenticore:queue` Redis list depth
(one scale-up per 5 queued jobs).

## StatefulSet Configuration

Key environment variables injected into pods:

| Variable | Value | Source |
|----------|-------|--------|
| `AGENTICORE_POD_NAME` | `metadata.name` | Downward API |
| `AGENTICORE_SHARED_FS_ROOT` | `/shared` | Manifest |
| `AGENTICORE_REPOS_ROOT` | `/shared/repos` | Manifest |
| `AGENTICORE_JOBS_DIR` | `/shared/job-state` | Manifest |
| `AGENTICORE_TRANSPORT` | `sse` | Manifest |
| `REDIS_URL` | from Secret | `agenticore-secrets` |

`AGENTICORE_POD_NAME` is set via Downward API so the pod's stable name
(`agenticore-0`, `agenticore-1`, …) is recorded on every job it runs.

## Pod Identity

StatefulSet pods have stable DNS names:

```
agenticore-0.agenticore-headless.default.svc.cluster.local
agenticore-1.agenticore-headless.default.svc.cluster.local
```

`pod_name` is stored on every job record so you can trace which pod ran a job:

```bash
agenticore jobs --json | jq '.[].pod_name'
```

## Graceful Shutdown

The StatefulSet sets `terminationGracePeriodSeconds: 300`. The PreStop hook
calls `agenticore drain --timeout 270` which:

1. Marks the pod as draining in Redis
2. Waits for all in-progress jobs to complete
3. Exits — Kubernetes then sends SIGTERM to the container

This ensures Claude sessions are never interrupted mid-task.

## KEDA Autoscaling

`keda-scaledobject.yaml` watches the `agenticore:queue` Redis list and adds a
replica for every 5 pending jobs, up to 10 pods.

```yaml
triggers:
- type: redis
  metadata:
    listName: agenticore:queue
    listLength: "5"
```

Adjust `minReplicaCount`, `maxReplicaCount`, and `listLength` to match your
throughput requirements.

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

# Scale test: submit 10 jobs and verify spread
for i in $(seq 1 10); do
  agenticore run "task $i" --repo https://github.com/example/repo
done
agenticore jobs | head -15
```

## Migration from Docker Compose

Docker Compose mode remains fully supported. `AGENTICORE_SHARED_FS_ROOT` is
unset by default, so `materialize_profile()` falls back to the existing
copy-into-repo behaviour. No code changes are needed to continue using Docker
Compose.

| Feature | Docker Compose | Kubernetes |
|---------|---------------|------------|
| Shared FS | No (local volume) | RWX PVC |
| Clone locking | `fcntl` flock | Redis `SET NX` |
| Profile materialization | Into repo working dir | `/shared/jobs/{id}/` |
| Pod identity | hostname | StatefulSet name (Downward API) |
| Scaling | Single container | KEDA ScaledObject |
| Drain | N/A | `agenticore drain` PreStop hook |

## Helm Chart

A Helm chart (`helm/agenticore/`) is also provided for simpler single-replica
deployments. It uses ReadWriteOnce PVCs and does not include the StatefulSet or
KEDA integration. For horizontal scaling, use the `k8s/` raw manifests above.
