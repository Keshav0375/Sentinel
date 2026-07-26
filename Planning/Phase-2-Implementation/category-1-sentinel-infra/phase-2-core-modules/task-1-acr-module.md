# task-1 — ACR module   ·   [category-1-sentinel-infra / phase-2-core-modules]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Phase branch** | `dev/infra-phase-2-core-modules` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §3.1 |
| **Depends on** | [[task-1-repo-skeleton-and-providers]] |
| **Referenced by** | [[task-1-aks-module]] (AcrPull), [[task-1-cross-repo-secrets]] (ACR creds), [[task-2-ci-runner-image]] |

## Spec
Standard-tier Container Registry that stores `sentinel-backend` + `ci-runner` images.

**Files created:** `modules/acr/{main.tf,variables.tf,outputs.tf}`
- `main.tf` — `azurerm_container_registry "sentinel"` name `sentinelacr`, sku `Standard`, `admin_enabled = true`.
- `variables.tf` — `resource_group_name`, `location`.
- `outputs.tf` — `acr_login_server`, `acr_admin_username`, `acr_admin_password` (sensitive).

**Contract:** name `sentinelacr` (globally unique — adjust if taken, propagate to R-notes).

## Prerequisites
- [ ] terraform CLI. [ ] task 1.1 root scaffold. [ ] ACR name available (verify at apply, ⛔ B1).

## Acceptance Criteria
- [ ] Module validates + fmt clean; outputs expose login server + admin creds (sensitive).
- [ ] Root `main.tf` calls the module (wired fully in task 4.4).

## Tests
- **Validate:** `terraform validate`, `tflint`, `tfsec` (flag admin_enabled as accepted dev risk).
- **Integration (⛔ B1):** `terraform apply -target=module.acr` → registry exists, `az acr show`.
- **Quality gate:** `--repo infra`.

## How to Verify (phase gate)
1. `terraform plan -target=module.acr` → creates 1 registry, no errors.
2. (post-apply) `az acr show --name sentinelacr` returns the registry.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Apply/verify ⛔ B1. Code + validate writable now._
