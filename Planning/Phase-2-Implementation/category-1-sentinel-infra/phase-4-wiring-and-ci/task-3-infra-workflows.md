# task-3 — Infra workflows (dry / apply / destroy / runners)   ·   [category-1-sentinel-infra / phase-4-wiring-and-ci]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Phase branch** | `dev/infra-phase-4-wiring-and-ci` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §6.3, §7.1, §7.2, §7.3 |
| **Depends on** | [[task-3-oidc-federation]], [[task-2-ci-runner-image]] |
| **Referenced by** | ongoing infra apply |

> ⚠ **rev-5 (2026-07-12):** **four** workflows, not three — `ci_destroy_infra.yml` was added
> (§7.3). And **no `db_password`** repo secret/variable anywhere: Postgres is Entra-only, so the
> dry-run passes only `github_pat`. See sentinel-infra §7.

## Spec
Four GHA workflows in `.github/workflows/`.

**Files created:**
- `ci_infra_dry.yml` — `[infra] terraform — validate and plan`; push (all branches) + PR to main; OIDC login; init + validate + `fmt -check` + plan; post plan to PR comment (§7.1). Vars: `github_pat` from repo secrets (**no `db_password`** — Entra-only Postgres).
- `ci_infra.yml` — `[infra] terraform — apply`; push to main; `environment: production`; OIDC login; init + `apply -auto-approve` (§7.2).
- `ci_destroy_infra.yml` — `[infra] terraform — destroy (full teardown)`; **`workflow_dispatch` only**, typed `DESTROY` confirmation input + `environment: destroy` protection; two stages — `terraform destroy` (all state-managed resources incl. the imported OIDC app + federated creds) then `az group delete` for `sentinel-rg` + `sentinel-state-rg`. Acquire the ARM token at job start so the destroy survives deleting its own identity (§7.3).
- `ci_runners.yml` — `[infra] runners — build and push`; push to main paths `ci-images/**`; OIDC login → `az acr login` → build + push `ci-runner:latest` (§6.3).
- Job IDs `kebab-case`; names Title case per convention.

## Prerequisites
- [ ] actionlint installed. [ ] task 1.3 OIDC + infra repo secrets (⛔ B3) for real runs; ⛔ B1.

## Acceptance Criteria
- [ ] **Four** workflows validate under actionlint; correct triggers/permissions (`id-token: write`).
- [ ] Names/job-ids follow the cross-repo convention.
- [ ] `ci_infra.yml` uses `environment: production` (optional manual approval gate).
- [ ] `ci_destroy_infra.yml` is `workflow_dispatch`-only, requires the typed `DESTROY` input, and
      is gated by the `destroy` environment — it can never fire from a push.
- [ ] No `db_password` secret or variable referenced by any workflow.

## Tests
- **Lint:** `actionlint .github/workflows/*.yml`, yamllint.
- **Integration (⛔ B1/B3):** open a PR → dry-run posts a plan comment; merge → apply runs.
  Do **not** live-test `ci_destroy_infra` until the user asks — it deletes everything.
- **Quality gate:** `--repo infra` — the infra matrix now runs `actionlint`, so this is covered.

## How to Verify (phase gate)
1. `actionlint` clean on all four.
2. (with secrets) a PR triggers dry-run + plan comment; merge triggers apply.
3. `ci_destroy_infra` appears in the Actions tab as manual-only and refuses to run without
   the typed `DESTROY` confirmation.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live runs ⛔ B1 + B3. YAML + actionlint now._
