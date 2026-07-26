# task-4 — Root wiring + outputs + end-to-end apply   ·   [category-1-sentinel-infra / phase-4-wiring-and-ci]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Phase branch** | `dev/infra-phase-4-wiring-and-ci` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §2, §4.3, §10 (full checklist) |
| **Depends on** | ALL infra module tasks (2.1–3.4), [[task-1-cross-repo-secrets]] |
| **Referenced by** | Category 2 & 3 (everything downstream) |

## Spec
Wire every module in the root `main.tf`, resolve inter-module dependencies (ACR→AKS,
Function→Event Grid, KV←SP), finalize `outputs.tf`, and drive a full clean `apply` from zero.

**Files modified:** `main.tf` (all module calls + `depends_on` ordering), `outputs.tf`
(acr_login_server, db_host, aks_cluster_name, app_url, key_vault_uri, function_app_id,
event grid endpoint), `README.md` (the §10 bootstrap runbook, ordered).

**Contract:** `terraform apply` from a clean state provisions the entire stack; `terraform
destroy` tears it down. Post-apply: §10 steps 6 (populate KV runtime secrets), 7 (push first
ci-runner), 10 (`alembic upgrade head` — backend task 1.3).

## Prerequisites
- [ ] All module tasks `done-pending-review`. [ ] task 1.2 state, task 1.3 OIDC. [ ] ⛔ B1–B9 for a real end-to-end apply.

## Acceptance Criteria
- [ ] `terraform validate` + `plan` for the whole stack is clean and ordered (no cycle).
- [ ] `terraform apply` from zero succeeds; all outputs populated.
- [ ] README runbook reproduces the bootstrap in order.
- [ ] This is the **phase-4 gate feature**: a working, reproducible Azure stack.

## Tests
- **Validate:** full `terraform validate`, tflint, tfsec, gitleaks on the whole repo.
- **Integration (⛔ B1–B9):** clean apply; smoke each resource (`az acr show`, `psql`, `kubectl get nodes`, app URL, `az keyvault secret list`, fire Event Grid → dispatch).
- **Quality gate:** `--repo infra`.

## How to Verify (phase gate — this is the big one for Category 1)
1. `terraform plan` on the full stack → no errors, sane resource count.
2. `terraform apply` from zero → green; run the README smoke checks for every resource.
3. Confirm cross-repo secrets landed (`gh secret list`) and KV holds the runtime secrets.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Full apply ⛔ B1–B9. Wiring + validate/plan writable now (offline validate)._
