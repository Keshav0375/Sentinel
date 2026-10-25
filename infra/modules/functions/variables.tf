variable "resource_group_name" {
  description = "Resource group for the plan, storage and function apps. Supplied by the root."
  type        = string
}

variable "location" {
  description = "Azure region. Supplied by the root from var.location — no default anywhere (R3)."
  type        = string
}

variable "storage_account_name" {
  description = "Globally unique storage account backing the Consumption plan (0375 convention; the architecture referenced this account but never named it)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.storage_account_name))
    error_message = "storage_account_name must be 3-24 lowercase alphanumeric characters."
  }
}

variable "bridge_name" {
  description = "Globally unique function app name — becomes <name>.azurewebsites.net."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,58}[a-z0-9]$", var.bridge_name))
    error_message = "bridge_name must be 2-60 chars, lowercase alphanumeric or hyphen."
  }
}

variable "key_vault_name" {
  description = "Vault name for the GITHUB_TOKEN Key Vault reference. From the root's var.key_vault_name — one source of truth for the globally-unique name."
  type        = string
}

variable "rotator_name" {
  description = "Globally unique rotator function app name."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,58}[a-z0-9]$", var.rotator_name))
    error_message = "rotator_name must be 2-60 chars, lowercase alphanumeric or hyphen."
  }
}
