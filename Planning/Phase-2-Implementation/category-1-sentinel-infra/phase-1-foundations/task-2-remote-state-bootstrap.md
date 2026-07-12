# task-2 — Remote state bootstrap (script + doc)   ·   [category-1-sentinel-infra / phase-1-foundations]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Local path** | `Agentic-Engineering/Sentinel-development-project/Sentinel-infra` |
| **Phase branch** | `impl/infra-phase-1-foundations` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §8.1, §8.2, §10 (bootstrap) |
| **Depends on** | [[task-1-repo-skeleton-and-providers]] |
| **Referenced by** | [[task-3-oidc-federation]], [[task-4-root-wiring-and-apply]] |

## Spec
Terraform's `azurerm` backend needs a state RG + storage account + container that exist
*before* the first `init`. This is a one-time manual bootstrap — scripted + documented,
not applied by Terraform (chicken-and-egg).

**Files created:**
- `scripts/bootstrap-state.sh` — idempotent: `az group create sentinel-state-rg`,
  `az storage account create sentineltfstate --sku Standard_LRS --encryption-services blob`,
  `az storage container create tfstate`. Params via env/flags; guards on existence.
- `docs/BOOTSTRAP.md` (or README section) — the ordered one-time setup (§10 steps 1–2),
  state locking note (blob leases, §8.2), and how `backend.tf` maps to these names.

**Contract:** names must match `backend.tf` from task 1: `sentinel-state-rg`,
`sentineltfstate`, `tfstate`, key `sentinel.terraform.tfstate`.

## Prerequisites
- [ ] `az` CLI installed + `az login` (⛔ B1: Azure subscription).
- [ ] Storage account name globally unique (adjust if `sentineltfstate` taken).

## Acceptance Criteria
- [ ] `bootstrap-state.sh` runs clean and is re-runnable (idempotent).
- [ ] `terraform init` (real, with backend) succeeds against the created container.
- [ ] BOOTSTRAP.md lists exact commands in order.

## Tests
- **Script:** `bash -n scripts/bootstrap-state.sh` (syntax) + shellcheck.
- **Integration (blocked on B1):** run the script, then `terraform init` resolves the backend.
- **Quality gate:** `--repo infra`.

## How to Verify (phase gate)
1. Run `scripts/bootstrap-state.sh` → state RG + storage + container exist (`az storage container list`).
2. `terraform init` → "Successfully configured the backend".

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED   ·   _only if halted_
_Verification BLOCKED on B1 (Azure subscription) + B2. Script + docs are writable now._
