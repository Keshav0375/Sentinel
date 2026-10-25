output "acr_login_server" {
  description = "Registry hostname, e.g. sentinelacr0375.azurecr.io. Used in image tags and by docker login."
  value       = azurerm_container_registry.sentinel.login_server
}

# Needed by the AKS module in phase 3 task 3.1: the AcrPull role assignment is
# scoped to the registry's resource id. Not listed in §3.1's original output set —
# added here because task-1-aks-module.md declares acr_id as an input and phase 3
# cannot wire without it.
output "acr_id" {
  description = "Registry resource id. Scope for the AcrPull role assignment granted to the AKS kubelet identity (§3.7)."
  value       = azurerm_container_registry.sentinel.id
}

output "acr_admin_username" {
  description = "Admin username for docker login from GHA. AKS does not use this — it pulls via AcrPull."
  value       = azurerm_container_registry.sentinel.admin_username
}

output "acr_admin_password" {
  description = "Admin password. Seeded into Key Vault as the `acr-password` secret (§3.3) and consumed by GHA docker login."
  value       = azurerm_container_registry.sentinel.admin_password
  sensitive   = true
}
