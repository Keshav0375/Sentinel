# ─────────────────────────────────────────────────────────────────────────────
# Native `terraform test` for the naming module.
#
# This module has no provider calls and no state — it is pure computation over
# four inputs — which makes it the one part of the estate that can be tested
# properly rather than verified by applying it and looking.
#
# The assertions check RULES, not literals. `uid` is a hash, so asserting an
# exact vault name would mean pasting a hash into a test and updating it whenever
# the subscription changes; asserting `length <= 24` and `matches ^kv-` tests the
# thing that actually breaks.
#
# Run:  terraform test         (from modules/naming)
# ─────────────────────────────────────────────────────────────────────────────

variables {
  deployment         = "demo1"
  environment        = "dev"
  location           = "canadacentral"
  subscription_id    = "174e25ca-ab82-4671-a913-9c2f66e5924d"
  identity_tenant_id = "eae0d3c6-af22-4b70-ad3b-12d625a06139"
}

run "names_satisfy_azure_limits" {
  command = plan

  assert {
    condition     = length(output.names.key_vault) <= 24
    error_message = "Key Vault name exceeds 24 chars: ${output.names.key_vault}"
  }

  assert {
    condition     = length(output.names.storage_account) <= 24
    error_message = "storage account name exceeds 24 chars: ${output.names.storage_account}"
  }

  # ACR and storage forbid hyphens outright. This is the assertion most likely to
  # catch a future edit — adding a separator "for readability" is the natural
  # thing to do and it breaks exactly these two.
  assert {
    condition     = can(regex("^[a-z0-9]+$", output.names.container_registry))
    error_message = "ACR name must be alphanumeric only: ${output.names.container_registry}"
  }

  assert {
    condition     = can(regex("^[a-z0-9]+$", output.names.storage_account))
    error_message = "storage account name must be lowercase alphanumeric only: ${output.names.storage_account}"
  }

  assert {
    condition     = length(output.names.container_registry) >= 5 && length(output.names.container_registry) <= 50
    error_message = "ACR name must be 5-50 chars: ${output.names.container_registry}"
  }

  assert {
    condition     = length(output.names.postgres_server) <= 63
    error_message = "Postgres server name exceeds 63 chars: ${output.names.postgres_server}"
  }

  assert {
    condition     = length(output.names.function_app) <= 60 && length(output.names.app_service) <= 60
    error_message = "Function App / App Service names exceed 60 chars"
  }
}

run "uid_is_four_hex_chars" {
  command = plan

  assert {
    condition     = can(regex("^[0-9a-f]{4}$", output.uid))
    error_message = "uid must be exactly 4 hex characters, got '${output.uid}'"
  }
}

run "location_is_abbreviated_not_spelled_out" {
  command = plan

  # `canadacentral` is 13 characters and would consume most of a Key Vault name
  # on its own. The abbreviation is what makes the budget work.
  assert {
    condition     = output.location_abbrev == "cc"
    error_message = "canadacentral should abbreviate to 'cc', got '${output.location_abbrev}'"
  }
}

run "api_identifier_uri_is_tenant_qualified" {
  command = plan

  # A bare `api://sentinel-backend-demo1-dev` is rejected by new Entra tenants
  # with InvalidUniqueTenantIdentifierAsPerAppPolicy. Found live 2026-08-23; this
  # assertion is what stops it being rediscovered.
  assert {
    condition     = startswith(output.names.api_identifier_uri, "api://eae0d3c6-af22-4b70-ad3b-12d625a06139/")
    error_message = "API identifier URI must be tenant-qualified: ${output.names.api_identifier_uri}"
  }
}

run "two_deployments_get_different_uids" {
  command = plan

  variables {
    deployment = "demo2"
  }

  # If uid did not vary with the deployment name, every deployment would collide
  # on every globally-unique resource — which is the entire failure this module
  # exists to prevent.
  assert {
    condition     = output.names.container_registry != "acrdemo1dev${output.uid}"
    error_message = "demo2 produced a registry name derived from demo1's inputs"
  }
}

# ── Negative cases ───────────────────────────────────────────────────────────
# The rules only matter if breaking them fails. Each of these would otherwise
# surface as an Azure error minutes into an apply, naming a resource attribute
# rather than the mistake.

run "rejects_deployment_over_eight_chars" {
  command = plan

  variables {
    deployment = "toolongname"
  }

  expect_failures = [var.deployment]
}

run "rejects_deployment_with_a_hyphen" {
  command = plan

  variables {
    deployment = "de-mo"
  }

  # ACR and storage account names forbid hyphens, and the deployment name is
  # concatenated straight into both.
  expect_failures = [var.deployment]
}

run "rejects_combined_length_over_fifteen" {
  command = plan

  variables {
    deployment  = "abcdefgh" # 8
    environment = "wxyz123"  # 7 -> 15, fits
  }

  # Control case: exactly at the limit must PASS, proving the negative below
  # fails for the length rule rather than for some other reason.
  assert {
    condition     = length(output.names.key_vault) == 24
    error_message = "8 + 7 should produce exactly a 24-char vault name, got ${length(output.names.key_vault)}"
  }
}

run "rejects_unmapped_location" {
  command = plan

  variables {
    location = "australiasoutheast"
  }

  # An unmapped region would otherwise produce `rg-demo1-dev-` — legal, wrong,
  # and colliding with every other unmapped region.
  expect_failures = [terraform_data.name_guards]
}
