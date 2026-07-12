# task-3 — Infra workflows (dry / apply / runners)   ·   [category-1-sentinel-infra / phase-4-wiring-and-ci]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Phase branch** | `impl/infra-phase-4-wiring-and-ci` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §6.3, §7.1, §7.2 |
| **Depends on** | [[task-3-oidc-federation]], [[task-2-ci-runner-image]] |
| **Referenced by** | ongoing infra apply |

## Spec
Three GHA workflows in `.github/workflows/`.

**Files created:**
- `ci_infra_dry.yml` — `[infra] terraform — validate and plan`; push (all branches) + PR to main; OIDC login; init + validate + `fmt -check` + plan; post plan to PR comment (§7.1). Vars: `db_password`, `github_pat` from repo secrets.
- `ci_infra.yml` — `[infra] terraform — apply`; push to main; `environment: production`; OIDC login; init + `apply -auto-approve` (§7.2).
- `ci_runners.yml` — `[infra] runners — build and push`; push to main paths `ci-images/**`; OIDC login → `az acr login` → build + push `ci-runner:latest` (§6.3).
- Job IDs `kebab-case`; names Title case per convention.

## Prerequisites
- [ ] actionlint installed. [ ] task 1.3 OIDC + infra repo secrets (⛔ B3) for real runs; ⛔ B1.

## Acceptance Criteria
- [ ] Three workflows validate under actionlint; correct triggers/permissions (`id-token: write`).
- [ ] Names/job-ids follow the cross-repo convention.
- [ ] `ci_infra.yml` uses `environment: production` (optional manual approval gate).

## Tests
- **Lint:** `actionlint .github/workflows/*.yml`, yamllint.
- **Integration (⛔ B1/B3):** open a PR → dry-run posts a plan comment; merge → apply runs.
- **Quality gate:** `--repo infra` (actionlint via infra matrix? add to gate or run standalone — see note).

## How to Verify (phase gate)
1. `actionlint` clean on all three.
2. (with secrets) a PR triggers dry-run + plan comment; merge triggers apply.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live runs ⛔ B1 + B3. YAML + actionlint now._
