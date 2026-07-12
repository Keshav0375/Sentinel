# task-1 — Repo skeleton + provider/backend/vars config   ·   [category-1-sentinel-infra / phase-1-foundations]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Local path** | `Agentic-Engineering/Sentinel-development-project/Sentinel-infra` |
| **Phase branch** | `impl/infra-phase-1-foundations` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §2, §8.1 |
| **Depends on** | — |
| **Referenced by** | [[task-3-oidc-federation]], all phase-2/3/4 module tasks |

## Spec
Lay down the root Terraform layout that every module wires into. No resources yet beyond
the provider/backend plumbing.

**Files created:**
- `main.tf` — `terraform{}` required_providers (`azurerm`, `azuread`, `github`), provider blocks (`azurerm` features{}, `github` owner=`Keshav0375`), `data "azurerm_client_config" "current"`, and the `azurerm_resource_group` reference (name from var). Module call stubs commented for phases 2–3.
- `variables.tf` — `subscription_id`, `location` (default `eastus`), `resource_group_name` (default `sentinel-rg`), `db_password` (sensitive, no default), `github_pat` (sensitive), `github_owner` (default `Keshav0375`).
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
- [ ] Decide region (R3) — default `eastus`.

## Acceptance Criteria
- [ ] `terraform fmt -check -recursive` clean.
- [ ] `terraform init -backend=false && terraform validate` passes (offline validate; real init needs state — task 1.2 / B2).
- [ ] `terraform.tfvars` and `.env` are gitignored.
- [ ] Provider `github.owner` = `Keshav0375`.

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
