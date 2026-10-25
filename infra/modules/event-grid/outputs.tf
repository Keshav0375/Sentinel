output "topic_id" {
  description = "Topic resource id."
  value       = azurerm_eventgrid_topic.sentinel.id
}

output "topic_name" {
  description = "Topic name — the root's event_grid_topic_name, for apply.sh's key lookup."
  value       = azurerm_eventgrid_topic.sentinel.name
}

output "topic_endpoint" {
  description = "Ingest URL — the Datadog webhook posts here (root event_grid_endpoint)."
  value       = azurerm_eventgrid_topic.sentinel.endpoint
}

output "topic_key" {
  description = "Access key for the Datadog webhook's aeg-sas-key header. Sensitive and deliberately NOT re-exported by the root: Sentinel-deployment datadog/apply.sh reads it live with `az eventgrid topic key list` — never echoed."
  value       = azurerm_eventgrid_topic.sentinel.primary_access_key
  sensitive   = true
}
