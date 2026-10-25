# ─────────────────────────────────────────────────────────────────────────────
# One map, plus the two derived values callers legitimately need.
#
# A single `names` object rather than twenty outputs: adding a resource type
# should not mean editing this file, and callers write `module.naming.names.key_vault`
# which reads as what it is.
# ─────────────────────────────────────────────────────────────────────────────

output "names" {
  description = "Every resource name for this deployment/environment, keyed by resource type."
  value       = local.names

  # Nothing is consumed until the guards have run. Without this, a caller could
  # read a name from a module whose preconditions had not yet been evaluated —
  # the guards would still fail the apply, but only after the caller had already
  # passed the bad name to a provider.
  depends_on = [terraform_data.name_guards]
}

output "uid" {
  description = "The 4-char uniqueness suffix. Exposed for diagnostics — if two deployments collide on a global name, this is the value to compare."
  value       = local.uid
}

output "location_abbrev" {
  description = "The region abbreviation used in names, e.g. `cc` for canadacentral."
  value       = local.loc
}
