variable "tenant_id" {
  description = "Entra tenant id. Passed explicitly by the root rather than read from data.azurerm_client_config, so an ambient az context switch cannot repoint it."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group to create the vault in. Supplied by the root from data.azurerm_resource_group.sentinel.name."
  type        = string
}

variable "location" {
  description = "Azure region. Supplied by the root from var.location — no default anywhere (R3)."
  type        = string
}

variable "vault_name" {
  description = "Globally unique vault name — it becomes <name>.vault.azure.net. A variable rather than a literal so a collision is a tfvars change; the unsuffixed `sentinel-kv` was already taken by another tenant."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9-]{1,22}[a-zA-Z0-9]$", var.vault_name))
    error_message = "vault_name must be 3-24 chars, alphanumeric or hyphen, start with a letter and not end with a hyphen."
  }
}

variable "kv_admin_object_id" {
  description = "Object ID of the HUMAN operator who seeds secrets (§10 step 7). Explicit rather than data.azurerm_client_config.current.object_id, which would resolve to the CI identity when CI applies and destroy the human's access."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$", var.kv_admin_object_id))
    error_message = "kv_admin_object_id must be a GUID (an object ID, not a UPN)."
  }
}

variable "seeder_principal_id" {
  description = "Principal granted Key Vault Secrets Officer so the deploy workflow can seed secrets after apply. Empty disables the grant. Distinct from kv_admin_object_id, which is the human: both hold Officer, neither replaces the other."
  type        = string
  default     = ""
}

variable "ci_reader_principal_id" {
  description = "Principal granted Key Vault Secrets User so a backend workflow can read LLM/Datadog keys at incident time. Empty disables the grant — the consuming workflow and its identity do not exist until the backend phase."
  type        = string
  default     = ""
}

# Deferred to phase 3, same pattern as the Postgres backend admin (task 2.2).
# Bool toggles rather than null-checks: `count` must resolve at PLAN time, and
# the phase-3 caller passes ids that are only known after apply.
variable "enable_backend_reader" {
  description = "Whether to grant the backend UAMI read access. Set true by the root from phase 3 task 3.1 onward."
  type        = bool
  default     = false
}

variable "enable_bridge_reader" {
  description = "Whether to grant the bridge Function's system-assigned identity read access. Set true by the root from phase 3 task 3.3 onward — without it the bridge's GITHUB_TOKEN Key Vault reference resolves silently empty."
  type        = bool
  default     = false
}

variable "bridge_principal_id" {
  description = "System-assigned principal of the bridge Function. Required when enable_bridge_reader is true; may be unknown at plan time."
  type        = string
  default     = null

  validation {
    condition     = !var.enable_bridge_reader || var.bridge_principal_id != null
    error_message = "enable_bridge_reader is true, so bridge_principal_id must be set (task 3.3 supplies it)."
  }
}

variable "enable_rotator_officer" {
  description = "Whether to grant the rotator Function write access. Set true by the root from phase 3 task 3.6 onward."
  type        = bool
  default     = false
}

variable "backend_uami_principal_id" {
  description = "Principal ID of the backend pod's workload-identity UAMI. Required when enable_backend_reader is true; may be unknown at plan time."
  type        = string
  default     = null

  # Without this, enabling the toggle without the id fails as
  # "Missing required argument" — true, but naming the wrong cause.
  validation {
    condition     = !var.enable_backend_reader || var.backend_uami_principal_id != null
    error_message = "enable_backend_reader is true, so backend_uami_principal_id must be set (phase 3 task 3.1 supplies it)."
  }
}

variable "rotator_principal_id" {
  description = "Principal ID of the rotator Function's system-assigned identity. Required when enable_rotator_officer is true."
  type        = string
  default     = null

  validation {
    condition     = !var.enable_rotator_officer || var.rotator_principal_id != null
    error_message = "enable_rotator_officer is true, so rotator_principal_id must be set (phase 3 task 3.6 supplies it)."
  }
}

variable "rotator_app_id" {
  description = "Rotator function app resource id, for the SecretNearExpiry subscription endpoint. Required when enable_rotator_officer is true."
  type        = string
  default     = null
}
