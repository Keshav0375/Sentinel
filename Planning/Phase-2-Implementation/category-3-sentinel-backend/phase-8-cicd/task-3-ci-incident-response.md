# task-3 — `ci_incident_response.yml` (real pipeline)   ·   [category-3-sentinel-backend / phase-8-cicd]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `impl/backend-phase-8-cicd` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §9.3 (full job DAG + YAML), §8.5, §13.6 |
| **Depends on** | [[task-3-composite-actions]], [[task-1-webhook-receiver]], [[task-3-generate-pr-content-endpoint]]; infra Function bridge (3.3); deployment monitors (2.3) |
| **Referenced by** | the whole system's end-to-end demo |

## Spec
The real incident pipeline — the payoff workflow. `repository_dispatch: incident-alert`,
scale-to-zero, serialized. Backend reasons, GHA executes (§9.3).

**Files created:** `.github/workflows/ci_incident_response.yml` — name `[sentinel] incident — response pipeline`; `repository_dispatch [incident-alert]`; `permissions: id-token/contents`; `concurrency: sentinel-backend`.
- Jobs (§9.3 DAG): `ensure-backend-up` (backend-up → `backend-url`; failure → Teams "NOT processed" + fail loudly) → parallel `fetch-service-info`/`fetch-pr-details`/`fetch-datadog-logs` (each OIDC + own KV secret; outputs non-secret, truncated) → `run-agent-pipeline` (POST `/webhooks/incident` + poll 10-min cap → timeout degrades to escalation) → branch: rollback (`generate-pr-content` → `create-rollback-pr` on Keshav0375/Sentinel-deployment + psql UPDATE incidents pr_number/pr_url → `notify-rollback`) OR `notify-escalation` → `teardown-backend` (`if: always()`, skip on KEEP_WARM) → `summary` (Datadog event + Teams).
- Respect §13.6: each job = fresh VM; no secrets across job outputs.

## Prerequisites
- [ ] task 7.3 actions, 5.1/5.3 endpoints. [ ] infra 3.3 bridge, deployment 2.3 monitors. [ ] ⛔ B1/B4–B9 for a live run.

## Acceptance Criteria
- [ ] Validates; DAG matches §9.3; rollback + escalation branches correct; teardown always; timeout → escalation (never silent hang).
- [ ] `create-rollback-pr` targets `Keshav0375/Sentinel-deployment` (R1); records pr_number/pr_url via psql-exec.

## Tests
- **Lint:** actionlint, yamllint; workflow_dispatch dry test of branch logic where possible.
- **Integration (⛔ B1/B4–B9):** fire a Condition-C alert end-to-end → revert PR opens on sentinel-deployment + Teams notified; Condition low-confidence → escalation only.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. actionlint clean.
2. (full stack) a demo failure → dispatch → this workflow → revert PR on sentinel-deployment + Teams message; incident row has pr_number/pr_url.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live end-to-end ⛔ B1 + B4–B9 + upstream infra/deployment phases. YAML + lint now._
