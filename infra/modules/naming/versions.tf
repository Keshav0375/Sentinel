# ─────────────────────────────────────────────────────────────────────────────
# This module declares NO providers, and that is the point.
#
# It is pure computation over four strings — no Azure call, no credential, no
# state. That is what lets `terraform test` exercise it properly (nine cases,
# including the negative ones) instead of proving it by applying it and looking
# at the result.
#
# `terraform_data` is a built-in, not a provider resource, so it needs no
# `required_providers` block. It requires Terraform >= 1.4; the root is pinned
# to >= 1.9 and CI runs 1.15.8.
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_version = ">= 1.9"
}
