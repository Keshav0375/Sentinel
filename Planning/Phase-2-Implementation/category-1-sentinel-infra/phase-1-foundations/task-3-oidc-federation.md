# task-3 — OIDC federation (bootstrap + Terraform federated creds)   ·   [category-1-sentinel-infra / phase-1-foundations]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Local path** | `Agentic-Engineering/Sentinel-development-project/Sentinel-infra` |
| **Phase branch** | `dev/infra-phase-1-foundations` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §4 (all), §9 |
| **Depends on** | [[task-1-repo-skeleton-and-providers]], [[task-2-remote-state-bootstrap]] |
| **Referenced by** | [[task-1-cross-repo-secrets]], every workflow that does `azure/login` |

## Spec
Passwordless GitHub→Azure auth. One Azure AD app + SP with federated credentials for all
three repos, no stored client secret. Bootstrap the app + first credential by hand, import
into Terraform, then TF manages the rest.

**Files created:**
- `oidc.tf` — `azuread_application "sentinel_gha"`, `azuread_service_principal`,
  `azurerm_role_assignment` Contributor on the RG, and one
  `azuread_application_federated_identity_credential` per repo+trigger (§4.2):
  `Sentinel-infra` main+pr, `Sentinel` main+pr, `Sentinel-deployment` main.
- `scripts/bootstrap-oidc.sh` — the one-time `az ad app create` + SP + Contributor + first
  federated credential (§4.3), then prints the `terraform import` command.
- README/BOOTSTRAP: the import step + the 5 GitHub secrets to add to Sentinel-infra (§9).

**⚠ R1 — use real subjects:** `repo:Keshav0375/Sentinel-infra:ref:refs/heads/main`,
`repo:Keshav0375/Sentinel:...`, `repo:Keshav0375/Sentinel-deployment:ref:refs/heads/main`
(arch text shows `keshxvDev` — do NOT copy it).

**Contract:**
```
issuer    = https://token.actions.githubusercontent.com
audiences = ["api://AzureADTokenExchange"]
subject   = repo:Keshav0375/<Repo>:<ref|pull_request>
```

## Prerequisites
- [ ] `az` CLI + rights to create AD app registrations (⛔ B1, B3).
- [ ] Repo names on GitHub confirmed (R2).
- [ ] task 1.2 state exists (so TF can manage the imported app).

## Acceptance Criteria
- [ ] `oidc.tf` validates; subjects use `Keshav0375` + correct repo names/refs.
- [ ] Bootstrap script creates the app + first federated credential.
- [ ] `terraform import azuread_application.sentinel_gha <app-id>` succeeds; subsequent `plan` shows no destroy of the app.
- [ ] A dummy workflow `azure/login@v2` with CLIENT/TENANT/SUB IDs authenticates.

## Tests
- **Validate:** `terraform validate`, `tflint`.
- **Integration (blocked on B1/B3):** trigger `azure/login` from an infra PR → token exchange succeeds.
- **Quality gate:** `--repo infra`.

## How to Verify (phase gate)
1. `scripts/bootstrap-oidc.sh` → app + SP + first credential created.
2. `terraform import ...` then `terraform plan` → app not recreated; remaining federated creds planned to add.
3. Open a test PR → the infra dry-run workflow logs a successful OIDC `azure/login`.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED   ·   _only if halted_
_Verification BLOCKED on B1 + B3 (Azure account + AD app rights). Code writable now._
