# task-1 — Repo skeleton + provider/backend/vars config   ·   [category-1-sentinel-infra / phase-1-foundations]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Local path** | `Agentic-Engineering/Sentinel-development-project/Sentinel-infra` |
| **Phase branch** | `dev/infra-phase-1-foundations` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §2, §8.1 |
| **Depends on** | — |
| **Referenced by** | [[task-3-oidc-federation]], all phase-2/3/4 module tasks |

> ⚠ **rev-5 (2026-07-12):** **no `db_password` variable** — PostgreSQL is Entra-only
> (`password_auth_enabled=false`), so no password exists to pass in. The Entra admin group's
> object id takes its place. See sentinel-infra §3.2 and [[task-2-postgresql-module]].
>
> ⚠ **R3 is OPEN — `location` is a halting prerequisite.** Do **not** default the region to
> `eastus` on your own; B2ats_v2 (AKS) and F1 (App Service) free-tier availability is
> region-dependent. Confirm the region with the user first (STATE-IMPL → Open Reconciliations).

## Spec
Lay down the root Terraform layout that every module wires into. No resources yet beyond
the provider/backend plumbing.

**Files created:**
- `main.tf` — `terraform{}` required_providers (`azurerm`, `azuread`, `github`), provider blocks (`azurerm` features{}, `github` owner=`Keshav0375`), `data "azurerm_client_config" "current"`, and the `azurerm_resource_group` reference (name from var). Module call stubs commented for phases 2–3.
- `variables.tf` — `subscription_id`, `location` (**R3 — confirm with the user, do not default**), `resource_group_name` (default `sentinel-rg`), `postgres_entra_admin_group_object_id`, `github_pat` (sensitive), `github_owner` (default `Keshav0375`). **No `db_password`** (Entra-only Postgres).
- `outputs.tf` — empty scaffold with header comment (populated in task 4.4).
- `backend.tf` — `backend "azurerm"` block per §8.1 (state-rg / sentineltfstate / tfstate / sentinel.terraform.tfstate).
- `terraform.tfvars.example` — every non-secret var with placeholder values; real `terraform.tfvars` gitignored.
- `.gitignore` — `.terraform/`, `*.tfstate*`, `terraform.tfvars`, `.env`, `*.tfplan`.
- `versions.tf` (optional) — pin provider versions.

**Contract:**
```hcl
provider "github" { owner = var.github_owner }   # Keshav0375, NOT keshxvDev (R1)
terraform { backend "azurerm" { ... } }            # §8.1
```

## Prerequisites
- [ ] `terraform` CLI installed (for fmt/validate).
- [ ] Repo cloned locally (present, bare).
- [ ] ⛔ **B13** — `release-phase-2` branch exists in `Sentinel-infra` (the phase branches off it).
- [ ] ⛔ **R3 RESOLVED** — the user has named the Azure region. **Halt if open**; do not default it.

## Acceptance Criteria
- [ ] `terraform fmt -check -recursive` clean.
- [ ] `terraform init -backend=false && terraform validate` passes (offline validate; real init needs state — task 1.2 / B2). This is exactly what `quality_gate.py --repo infra` runs.
- [ ] `terraform.tfvars` and `.env` are gitignored.
- [ ] Provider `github.owner` = `Keshav0375`.
- [ ] No `db_password` variable anywhere; `location` carries the region the user confirmed.

## Tests
- **Unit/validate:** `terraform fmt -check`, `terraform validate` (with `-backend=false`).
- **Quality gate:** `python scripts/quality_gate.py --repo infra --path <repo>` (fmt · validate · tflint · gitleaks).

## How to Verify (phase gate)
1. `cd Sentinel-infra && terraform fmt -check -recursive` → clean.
2. `terraform init -backend=false && terraform validate` → "Success".

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED   ·   _only if halted_
_none — code is writable offline. Full `terraform init` with remote state is BLOCKED on B2 until task 1.2 bootstraps state storage._
