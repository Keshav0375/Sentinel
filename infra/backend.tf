# ─────────────────────────────────────────────────────────────────────────────
# Remote state (architecture/infra.md §8.1).
#
# State lives in its OWN resource group, not sentinel-rg, so that
# the Owner-run local teardown (§7.3, R6) cannot destroy the
# state that describes what it is destroying.
#
# Auth is Entra, not a storage access key:
#   use_azuread_auth  — reach the blob with a token
#   use_oidc          — in CI that token comes from the GitHub OIDC exchange
#
# This is why the sentinel-gha identity needs `Storage Blob Data Contributor` on
# the storage account (oidc.tf, task 1.3): the state account sits outside the
# Contributor assignment scoped to sentinel-rg. Note that Owner on the
# subscription does NOT imply blob data access — control plane and data plane are
# separate in Azure RBAC, so a human running this locally needs the same grant.
#
# subscription_id / tenant_id / client_id are deliberately absent: a backend block
# cannot interpolate variables, so they arrive as ARM_* env vars. The same file
# therefore works locally (`az login`) and in CI (federated token).
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  backend "azurerm" {
    resource_group_name  = "rg-sentinel-bootstrap"
    storage_account_name = "stsentineltfb7fa37"
    container_name       = "tfstate"
    key                  = "sentinel.tfstate"

    use_azuread_auth = true
    use_oidc         = true
  }
}
