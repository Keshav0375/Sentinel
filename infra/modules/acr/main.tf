# ─────────────────────────────────────────────────────────────────────────────
# Azure Container Registry (architecture/infra.md §3.1).
#
# Holds two images: `sentinel-backend` (sha-pinned, plus a `stable` rollback tag)
# and `ci-runner` for the self-hosted CI image. AcrPull is NOT granted here — it
# belongs to the AKS module (§3.7) so the grant lives with the consumer.
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  # Declared per-module rather than inherited. A module that implicitly relies on
  # the root's provider constraints breaks the moment it is consumed from
  # anywhere else, and tflint flags the omission. §2 mandates exactly three files
  # per module, so this lives in main.tf rather than a fourth versions.tf.
  required_version = ">= 1.9"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

resource "azurerm_container_registry" "sentinel" {
  # Alphanumeric only — ACR rejects hyphens — and globally unique across all of
  # Azure. The unsuffixed `sentinelacr` was already taken by another tenant, as
  # were `sentinel-pg` and `sentinel-kv`; the `0375` suffix is the project-wide
  # convention for globally-scoped names (see §3 naming conventions).
  name                = var.registry_name
  resource_group_name = var.resource_group_name
  location            = var.location

  # Standard. Briefly set to Basic on cost grounds, then corrected 2026-08-16 once
  # the actual entitlement was read: the subscription is AzureForStudents_2018-01-01,
  # and its 12-month free grant covers "1 **Standard** tier registry with 100 GB
  # storage and 10 webhooks". Basic is a *different meter* and is therefore not
  # covered — the cheaper SKU would have cost ~$5/mo where the pricier one costs $0.
  #
  # The general lesson: on a subsidised subscription, "cheaper SKU" and "free SKU"
  # are not the same question. Check the entitlement before optimising the price.
  sku = "Standard"

  # Admin user is enabled so GHA can `docker login` with a username/password pair
  # pulled from Key Vault (`acr-password`, §3.3). AKS itself does NOT use this —
  # it pulls via the AcrPull role assignment on its kubelet identity, so no
  # imagePullSecret is needed in the cluster.
  #
  # ACCEPTED RISK, stated because the Key Vault module asserts the opposite
  # invariant a few files away: enabling the admin user means Azure generates a
  # password and Terraform stores it IN STATE, in plaintext. The state blob is
  # readable by anyone with Storage Blob Data Contributor on sentinel-state-rg.
  # This is the one credential in the phase that lives in state, it is inherent
  # to admin_enabled, and the mitigation is rotation:
  #   az acr credential renew --name <registry> --password-name password
  # The `sensitive = true` on the output prevents console exposure; it does
  # nothing about state.
  admin_enabled = true
}
