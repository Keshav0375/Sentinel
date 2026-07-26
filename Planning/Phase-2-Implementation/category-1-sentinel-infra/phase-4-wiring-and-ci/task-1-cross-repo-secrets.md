# task-1 — Cross-repo secret distribution (github provider)   ·   [category-1-sentinel-infra / phase-4-wiring-and-ci]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Phase branch** | `dev/infra-phase-4-wiring-and-ci` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §5 |
| **Depends on** | [[task-1-acr-module]], [[task-3-oidc-federation]] |
| **Referenced by** | sentinel `ci_*` workflows, sentinel-deployment `ci_app_deployment` |

## Spec
After apply, push identity/ACR values to the sentinel + sentinel-deployment repos as GitHub
Actions secrets — no manual copy-paste.

**Files created:** `github-secrets.tf`
- To `Sentinel` (⚠ R1/R2 real name): `github_actions_secret` × 6 — ACR_LOGIN_SERVER, ACR_USERNAME, ACR_PASSWORD, AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID.
- To `Sentinel-deployment`: `github_actions_secret` × 3 — AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID.
- Values from ACR module outputs + `azuread_application.sentinel_gha.client_id` + client config tenant/subscription.

**Contract:** `provider "github" { owner = "Keshav0375" }` (from task 1.1). `repository` args use real repo names.

## Prerequisites
- [ ] task 2.1 ACR outputs, task 1.3 app client_id. [ ] ⛔ B9 GitHub PAT (`repo` scope) as infra secret. [ ] ⛔ B1 to apply.

## Acceptance Criteria
- [ ] Validates; 6 secrets → Sentinel, 3 → Sentinel-deployment; correct repo names (Keshav0375).
- [ ] No secret values printed in plan output (mark sensitive).

## Tests
- **Validate:** validate, tflint, gitleaks.
- **Integration (⛔ B1/B9):** apply; `gh secret list -R Keshav0375/Sentinel` shows the 6; `-R .../Sentinel-deployment` shows the 3.
- **Quality gate:** `--repo infra`.

## How to Verify (phase gate)
1. `terraform plan` → 9 github_actions_secret resources.
2. (post-apply) `gh secret list` on both repos shows the expected names.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Apply ⛔ B1 + B9. Code writable now._
