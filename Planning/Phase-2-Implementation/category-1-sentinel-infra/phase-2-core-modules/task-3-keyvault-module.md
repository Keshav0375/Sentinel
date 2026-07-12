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

## Spec
Central secret store, RBAC-authorized. Terraform writes, GHA SP reads.

**Files created:** `modules/keyvault/{main.tf,variables.tf,outputs.tf}`
- `azurerm_key_vault "sentinel"` — name `sentinel-kv`, sku `standard`, `enable_rbac_authorization = true`, tenant from client config.
- `azurerm_role_assignment "terraform_kv_admin"` — `Key Vault Secrets Officer` to `data.azurerm_client_config.current.object_id`.
- `azurerm_role_assignment "gha_kv_reader"` — `Key Vault Secrets User` to the GHA SP object id (from task 1.3 output).
- Secret placeholders (values loaded post-apply via `az keyvault secret set`, §10 step 6) OR `azurerm_key_vault_secret` where the value is a TF input (e.g. `db-password`). Document the 11 secrets from §3.3 table with their consumers.
- `variables.tf` — `resource_group_name`, `location`, `gha_sp_object_id`, `db_password`.
- `outputs.tf` — `key_vault_id`, `key_vault_name`, `key_vault_uri`.

**Secrets (§3.3):** anthropic-api-key, openai-api-key, db-password, dd-api-key, dd-app-key,
teams-webhook-url, langfuse-secret-key, langfuse-public-key, acr-password, github-pat, sentinel-api-token.

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
