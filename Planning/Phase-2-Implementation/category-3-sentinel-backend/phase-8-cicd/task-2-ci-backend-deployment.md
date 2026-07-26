# task-2 — `ci_backend_deployment.yml` (post-merge deploy)   ·   [category-3-sentinel-backend / phase-8-cicd]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-8-cicd` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §9.2, §8.2, §8.5 |
| **Depends on** | [[task-3-composite-actions]], [[task-2-k8s-manifests]]; infra ACR+AKS+KV |
| **Referenced by** | production backend deploys |

## Spec
Post-merge: build+push (immutable sha tag), scale up AKS, sync secrets, deploy, validate,
run all tests + smoke against the LIVE deployment, promote or `rollout undo`, scale to zero.
In the `sentinel-backend` concurrency group.

**Files created:** `.github/workflows/ci_backend_deployment.yml` — name `[sentinel] backend — deployment`; `push: main` (paths src/tests/pyproject/Dockerfile/azure/.github/actions); `concurrency: sentinel-backend`.
- Jobs (§9.2): `run-quality` → `build-and-push` (sha tag, no latest) → `deploy-to-aks` (secret sync §8.2, `backend-up` → `backend-url`, `kubectl set image`, rollout, validate `/health`+`/ready`) → `run-tests` (unit + integration + smoke POST canned incident to `{backend-url}/webhooks/incident`) → `promote-or-rollback` (`if: always()`: green → tag `stable` + ACR cleanup keep-3; red → `rollout undo` + notify-teams; always → `backend-down` unless KEEP_WARM).

## Prerequisites
- [ ] task 7.3 actions, 7.2 manifests, 7.1 image. [ ] ⛔ B1 (ACR/AKS/KV) + infra secrets (4.1) for live runs.

## Acceptance Criteria
- [ ] Validates; immutable sha tag only (no `latest` in hot path); rollback on any post-deploy failure; scale-to-zero teardown.
- [ ] Smoke test asserts incident stored + resolution/escalation + judge score present.

## Tests
- **Lint:** actionlint, yamllint.
- **Integration (⛔ B1):** merge to main → image built/pushed, deployed, tests+smoke pass, image promoted to `stable`, node scaled to 0.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. actionlint clean.
2. (with cloud) a merge deploys + validates + scales down; a deliberately broken merge triggers `rollout undo` + Teams alert.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live deploy ⛔ B1 + infra 4.1. YAML + lint now._
