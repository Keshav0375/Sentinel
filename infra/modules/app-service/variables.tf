variable "resource_group_name" {
  description = "Resource group for the plan + app. Supplied by the root."
  type        = string
}

variable "location" {
  description = "Azure region. Supplied by the root from var.location — no default anywhere (R3)."
  type        = string
}

variable "app_name" {
  description = "Globally unique Azure resource name (app-<dep>-<env>-<uid> from modules/naming) — becomes <name>.azurewebsites.net. Not the Datadog service name, which is sentinel-watchtower."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,58}[a-z0-9]$", var.app_name))
    error_message = "app_name must be 2-60 chars, lowercase alphanumeric or hyphen, not starting or ending with a hyphen."
  }
}
