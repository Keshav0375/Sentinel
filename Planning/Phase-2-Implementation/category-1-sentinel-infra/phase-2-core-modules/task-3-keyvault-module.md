# task-3 — Key Vault module (+ RBAC roles, secrets)   ·   [category-1-sentinel-infra / phase-2-core-modules]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Phase branch** | `impl/infra-phase-2-core-modules` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §3.3 |
| **Depends on** | [[task-2-postgresql-module]], [[task-3-oidc-federation]] (GHA SP object id) |
| **Referenced by** | [[task-1-aks-module]], [[task-2-ci-app-deployment]], [[task-3-composite-actions]] (get-kv-secrets), all runtime-secret consumers |

> ⚠ **rev-5 (2026-07-12):** secret inventory + RBAC changed. **No `db-password`** (Postgres is
> Entra-only) and **no `sentinel-api-token`** (Entra bearer). Add two more RBAC roles: backend
> UAMI = Secrets User, rotator Function MI = Secrets Officer. LLM keys get an expiry (rotation,
> task 3.6). No `db_password` input variable. See sentinel-infra §3.3.

## Spec
Central secret store, RBAC-authorized. Terraform writes, GHA SP + backend UAMI read, rotator writes.

**Files created:** `modules/keyvault/{main.tf,variables.tf,outputs.tf}`
- `azurerm_key_vault "sentinel"` — name `sentinel-kv`, sku `standard`, `enable_rbac_authorization = true`, tenant from client config.
- `azurerm_role_assignment "terraform_kv_admin"` — `Key Vault Secrets Officer` to `data.azurerm_client_config.current.object_id`.
- `azurerm_role_assignment "gha_kv_reader"` — `Key Vault Secrets User` to the GHA SP object id (task 1.3 output).
- `azurerm_role_assignment "backend_kv_reader"` — `Key Vault Secrets User` to the backend UAMI (task 3.1).
- `azurerm_role_assignment "rotator_kv_officer"` — `Key Vault Secrets Officer` to the rotator Function MI (task 3.6).
- Secret placeholders loaded post-apply via `az keyvault secret set` (§10). LLM keys seeded with a ~90d expiry (rotation, task 3.6).
- `variables.tf` — `resource_group_name`, `location`, `gha_sp_object_id`, `backend_uami_principal_id` (**no** `db_password`).
- `outputs.tf` — `key_vault_id`, `key_vault_name`, `key_vault_uri`.

**Secrets (§3.3, 7):** anthropic-api-key ↻, openai-api-key ↻, dd-api-key, dd-app-key,
teams-webhook-url, langfuse-secret-key, langfuse-public-key, acr-password, github-pat.
**Removed:** ~~db-password~~ (Entra DB auth), ~~sentinel-api-token~~ (Entra bearer).

## Prerequisites
- [ ] task 1.3 GHA SP object id available. [ ] task 2.2 db-password. [ ] ⛔ B1 to apply; ⛔ B4–B9 to populate runtime secrets.

## Acceptance Criteria
- [ ] Validates; RBAC (not access policies); two role assignments (Officer for TF, User for GHA SP).
- [ ] `db-password` written by TF; the 8 runtime secrets documented for `az keyvault secret set`.
- [ ] Outputs expose vault id/name/uri.

## Tests
- **Validate:** validate, tflint, tfsec, gitleaks (ensure no literal secret values committed).
- **Integration (⛔ B1):** apply; GHA SP can `az keyvault secret show db-password`; TF SP can set; GHA SP cannot set.
- **Quality gate:** `--repo infra`.

## How to Verify (phase gate)
1. `terraform plan -target=module.keyvault` → vault + 2 role assignments (+ db-password secret).
2. (post-apply) `az keyvault secret list --vault-name sentinel-kv`; confirm GHA SP read-only via `az role assignment list`.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Apply ⛔ B1. Populating runtime secret values ⛔ B4–B9. Code writable now._
