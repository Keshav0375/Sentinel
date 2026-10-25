# ─────────────────────────────────────────────────────────────────────────────
# Naming module inputs.
#
# Validation lives here rather than at the root because these four values are
# the ONLY inputs to every name in the estate. A bad value caught here fails in
# two seconds with a sentence; the same value caught by Azure fails eight minutes
# into an apply with `The storage account name is invalid`, naming a resource
# rather than the mistake.
# ─────────────────────────────────────────────────────────────────────────────

variable "deployment" {
  description = "Deployment name — the unit of isolation. Lowercase alphanumeric, starts with a letter, 2-8 chars."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,7}$", var.deployment))
    error_message = "deployment must be 2-8 chars, lowercase alphanumeric, starting with a letter. It feeds ACR and storage account names, which forbid hyphens entirely."
  }
}

variable "environment" {
  description = "Environment within the deployment — dev, prod, plat, …. Lowercase alphanumeric, 2-7 chars."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,6}$", var.environment))
    error_message = "environment must be 2-7 chars, lowercase alphanumeric, starting with a letter."
  }

  # THE BINDING CONSTRAINT, and it is a SUM rather than a per-value cap.
  #
  # Key Vault allows 24 characters. `kv-<d>-<env>-<uid>` spends 9 of them on
  # fixed structure: "kv-"(3) + "-"(1) + "-"(1) + uid(4). That leaves 15 to share
  # between the deployment and the environment.
  #
  # "deployment <= 8" is a CONSEQUENCE of this rule, not the rule — 8 holds only
  # while the environment is <= 7. Writing the cap instead of the sum is how you
  # get a valid-looking `deployment=platform, environment=staging` that produces
  # a 26-character vault name and fails at apply.
  validation {
    condition     = length(var.deployment) + length(var.environment) <= 15
    error_message = "length(deployment) + length(environment) must be <= 15. Key Vault names cap at 24 and `kv-<d>-<env>-<uid>` spends 9 on structure, leaving 15 to share."
  }
}

variable "location" {
  description = "Azure region. Must appear in the abbreviation map — see main.tf."
  type        = string
}

variable "subscription_id" {
  description = "Subscription ID. Feeds the uid hash, so names differ across subscriptions even for identical deployment/environment pairs."
  type        = string
}

variable "identity_tenant_id" {
  description = "The IDENTITY tenant (R4). Only used to build the tenant-qualified API identifier URI — new Entra tenants reject the bare `api://name` form."
  type        = string
}
