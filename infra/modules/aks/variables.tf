variable "resource_group_name" {
  description = "Resource group for the cluster and the backend UAMI. Supplied by the root."
  type        = string
}

variable "location" {
  description = "Azure region. Supplied by the root from var.location — no default anywhere (R3)."
  type        = string
}

variable "acr_id" {
  description = "Registry resource id — the scope of the kubelet's AcrPull grant. From module.acr.acr_id."
  type        = string
}

variable "cluster_name" {
  description = "Cluster name, from modules/naming. Unique per resource group only, so it carries no uid."
  type        = string
}

variable "dns_prefix" {
  description = "DNS prefix, from modules/naming. Unique per region; Azure appends its own hash."
  type        = string
}

variable "node_vm_size" {
  description = "Node SKU. B2pls_v2 is ARM64 — backend images must build linux/arm64."
  type        = string
  default     = "Standard_B2pls_v2"
}

variable "node_os_disk_size_gb" {
  description = "Node OS disk size. Billed as a provisioned Premium SSD, so size picks the tier: 64 GB is a P6, the Azure default of 128 GB is a P10 at roughly twice the price. AKS floor is 30."
  type        = number
  default     = 64

  validation {
    condition     = var.node_os_disk_size_gb >= 30
    error_message = "AKS requires an OS disk of at least 30 GB; got ${var.node_os_disk_size_gb}."
  }
}

variable "tenant_id" {
  description = "Entra tenant for Kubernetes RBAC. Pinned from the root rather than read from the ambient az context — a cross-tenant login would otherwise repoint the cluster's trust."
  type        = string
}
