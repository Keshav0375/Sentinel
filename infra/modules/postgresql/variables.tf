variable "tenant_id" {
  description = "Entra tenant id. Passed explicitly by the root rather than read from data.azurerm_client_config, so an ambient az context switch cannot repoint it."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group to create the server in. Supplied by the root from data.azurerm_resource_group.sentinel.name."
  type        = string
}

variable "location" {
  description = "Azure region. Supplied by the root from var.location — no default anywhere (R3)."
  type        = string
}

variable "server_name" {
  description = "Globally unique server name — it becomes <name>.postgres.database.azure.com. A variable rather than a literal so a collision is a tfvars change; the unsuffixed `sentinel-pg` was already taken by another tenant."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.server_name))
    error_message = "server_name must be 3-63 chars, lowercase alphanumeric or hyphen, and cannot start or end with a hyphen."
  }
}

variable "postgres_entra_admin_object_id" {
  description = "Object ID of the human Entra principal that administers the server (break-glass, and the principal that runs pgaadauth_create_principal for the other roles)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$", var.postgres_entra_admin_object_id))
    error_message = "postgres_entra_admin_object_id must be a GUID (an object ID, not a UPN)."
  }
}

variable "postgres_entra_admin_principal_name" {
  description = "UPN of that principal, e.g. you@uwindsor.ca. Azure requires it to match the object ID."
  type        = string
}

# Deferred to phase 3 task 3.1 — the backend UAMI does not exist until the AKS
# module creates it.
#
# The toggle is a SEPARATE bool, not `count = var.x == null ? 0 : 1`. That
# shorter form works only while the value is a literal `null`: in phase 3 the
# caller passes `module.aks.<uami>.principal_id`, which is UNKNOWN at plan time
# on the apply that creates it, and `count` on an unknown fails with
# "The count value depends on resource attributes that cannot be determined
# until apply". A bool the caller sets explicitly is always known at plan time,
# while the principal id it guards may legitimately be unknown.
variable "enable_backend_admin" {
  description = "Whether to attach the backend UAMI as a second Entra administrator. Set true by the root from phase 3 task 3.1 onward. Must be statically known at plan time — see the note above."
  type        = bool
  default     = false
}

variable "backend_uami_principal_id" {
  description = "Principal ID of the backend pod's workload-identity UAMI. Required when enable_backend_admin is true; may be unknown at plan time."
  type        = string
  default     = null

  validation {
    condition     = !var.enable_backend_admin || var.backend_uami_principal_id != null
    error_message = "enable_backend_admin is true, so backend_uami_principal_id must be set (phase 3 task 3.1 supplies it)."
  }
}

variable "sku_name" {
  description = "Compute SKU. B_Standard_B1ms is the cheapest burstable that fits the student grant."
  type        = string
  default     = "B_Standard_B1ms"
}

variable "storage_mb" {
  description = "Storage in MB. Azure allows growth but NEVER shrinking — a value set too high cannot be undone without recreating the server."
  type        = number
  default     = 32768
}

variable "database_name" {
  description = "Application database on this server. In `shared` mode each deployment gets its own database here rather than its own server."
  type        = string
  default     = "sentinel"
}

variable "backend_uami_principal_name" {
  description = "Display name of the workload identity attached as a second Entra administrator. Postgres stores it verbatim as the role name, so it must match the UAMI exactly."
  type        = string
  default     = "sentinel-backend-wi"
}
