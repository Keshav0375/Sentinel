output "app_url" {
  description = "Public URL of the target app — what the Datadog monitors watch and the scenario deploys hit."
  value       = "https://${azurerm_linux_web_app.dummy_api.default_hostname}"
}

output "app_id" {
  description = "Web app resource ID — the scope of gha-app's Website Contributor grant in the deployment layer."
  value       = azurerm_linux_web_app.dummy_api.id
}

output "app_name" {
  description = "Web app name — consumed by ci_app_deployment.yml (azure/webapps-deploy) and the cross-repo variable push (task 4.1)."
  value       = azurerm_linux_web_app.dummy_api.name
}
