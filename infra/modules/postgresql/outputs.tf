output "db_host" {
  description = "Fully qualified server hostname, e.g. sentinel-pg-0375.postgres.database.azure.com."
  value       = azurerm_postgresql_flexible_server.sentinel.fqdn
}

output "db_name" {
  description = "Application database name."
  value       = azurerm_postgresql_flexible_server_database.sentinel.name
}

output "db_port" {
  description = "PostgreSQL port. Constant, exposed so consumers do not hardcode it."
  value       = 5432
}

# There is deliberately NO password output. Entra-only auth means no
# administrator password exists to expose — clients acquire a short-lived token
# (audience https://ossrdbms-aad.database.windows.net) and present it as the psql
# password. Adding a password output here would require re-introducing password
# auth, which rev-5 removed on purpose.

output "server_id" {
  description = "Resource ID of the flexible server. The deployment layer addresses it to create a database on the SHARED server without holding the server itself in its own state."
  value       = azurerm_postgresql_flexible_server.sentinel.id
}
