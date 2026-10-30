output "hostname" {
  description = "Cache hostname. Resolves to the private endpoint from inside the AKS VNet."
  value       = azurerm_managed_redis.this.hostname
}

output "port" {
  description = "TLS port of the default database (10000 for Managed Redis)."
  value       = azurerm_managed_redis.this.default_database[0].port
}

output "id" {
  description = "Resource ID of the cache."
  value       = azurerm_managed_redis.this.id
}
