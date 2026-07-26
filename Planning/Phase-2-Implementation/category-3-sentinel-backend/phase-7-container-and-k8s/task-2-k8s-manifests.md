# task-2 — `azure/k8s/` deployment + service manifests   ·   [category-3-sentinel-backend / phase-7-container-and-k8s]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-7-container-and-k8s` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §8.1, §8.5, §13.3 |
| **Depends on** | [[task-1-dockerfile-update]], [[task-4-health-ready-probes]]; infra [[task-1-aks-module]] |
| **Referenced by** | [[task-3-composite-actions]] (backend-up applies these), [[task-2-ci-backend-deployment]] |

## Spec
The K8s manifests the backend-up action applies. Single replica; Service is created/deleted
per run (dynamic URL, §8.5).

**Files created:**
- `azure/k8s/deployment.yaml` — Deployment `sentinel-backend`, `replicas: 1`, `strategy: Recreate`, image `sentinelacr.azurecr.io/sentinel-backend:sha-PLACEHOLDER` (CI sets via `kubectl set image`), `envFrom: sentinel-secrets`, readiness `/ready` (initialDelay 10), liveness `/health` (initialDelay 15), resources requests 250m/512Mi limits 1/2Gi.
- `azure/k8s/service.yaml` — `Service type: LoadBalancer`, selector app=sentinel-backend, port 80→8000.

## Prerequisites
- [ ] task 7.1 image. [ ] kubectl (for `--dry-run` validation). [ ] AKS (⛔ B1) for a live apply.

## Acceptance Criteria
- [ ] `kubectl apply --dry-run=client -f azure/k8s/` validates both manifests.
- [ ] Probes point at `/ready` + `/health`; envFrom the K8s Secret; Recreate strategy (never 2 pods on the 4 GB node).

## Tests
- **Validate:** `kubectl apply --dry-run=client`; kubeconform/kubeval (optional); yamllint.
- **Integration (⛔ B1):** apply to AKS → pod Ready, LB IP assigned, `/health` reachable.
- **Quality gate:** `--repo backend` (+ manifest lint).

## How to Verify (phase gate)
1. `kubectl apply --dry-run=client -f azure/k8s/` → valid.
2. (with AKS) apply → `kubectl get pods` Ready; `kubectl get svc` shows an external IP serving `/health`.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live apply ⛔ B1. Manifests + dry-run validate now._
