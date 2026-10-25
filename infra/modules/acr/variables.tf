variable "resource_group_name" {
  description = "Resource group to create the registry in. Supplied by the root from data.azurerm_resource_group.sentinel.name."
  type        = string
}

variable "location" {
  description = "Azure region. Supplied by the root from var.location — no default anywhere (R3)."
  type        = string
}

variable "registry_name" {
  description = "Globally unique registry name, alphanumeric only (ACR rejects hyphens). Defaulted rather than hardcoded so a collision is a tfvars change, not a code change — the unsuffixed name was already taken by another tenant."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9]{5,50}$", var.registry_name))
    error_message = "registry_name must be 5-50 alphanumeric characters. ACR does not accept hyphens or underscores."
  }
}
