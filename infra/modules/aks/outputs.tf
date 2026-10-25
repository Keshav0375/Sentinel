output "aks_cluster_name" {
  description = "Cluster name, for az aks get-credentials / nodepool scale in the workflows."
  value       = azurerm_kubernetes_cluster.sentinel.name
}

output "aks_resource_group" {
  description = "Resource group of the cluster — the workflows take both as inputs."
  value       = azurerm_kubernetes_cluster.sentinel.resource_group_name
}

output "oidc_issuer_url" {
  description = "The cluster's OIDC issuer. The federated credential binds to it; exported for diagnostics and for backend task 7.2's docs."
  value       = azurerm_kubernetes_cluster.sentinel.oidc_issuer_url
}
