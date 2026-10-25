output "key_vault_id" {
  description = "Vault resource id. Scope for the phase-3 role assignments and the rotation Event Grid subscription."
  value       = azurerm_key_vault.sentinel.id
}

output "key_vault_name" {
  description = "Vault name, for `az keyvault secret set` in §10 step 7 and the GHA kv-secrets composite action."
  value       = azurerm_key_vault.sentinel.name
}

output "key_vault_uri" {
  description = "Vault URI, e.g. https://sentinel-kv-0375.vault.azure.net/. What the backend's azure-keyvault-secrets client connects to via workload identity."
  value       = azurerm_key_vault.sentinel.vault_uri
}

# No secret outputs, and no azurerm_key_vault_secret resources anywhere in this
# module. Terraform creates the vault and the access; the 9 values are seeded out
# of band (§10 step 7). A secret in HCL is a secret in plaintext state.
