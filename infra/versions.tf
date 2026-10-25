# ─────────────────────────────────────────────────────────────────────────────
# Version pins. Every module inherits these.
#
# azurerm is held at v4 deliberately: v4 makes `subscription_id` mandatory on the
# provider, which gives the root one explicit source for it instead of ambient
# ARM_* environment variables.
#
# ── Only two providers are declared, and that is load-bearing ────────────────
# Terraform CONFIGURES a declared provider whether or not any resource of that
# provider is planned — `count = 0` on every resource is not enough. So a
# provider that cannot authenticate breaks EVERY plan, including ones that never
# touch it, with an error about the wrong thing entirely.
#
#   azuread  removed 2026-08-25. The per-deployment app registrations are written
#            (commit 65e35b9) but cannot ship until `sentinel-tf-identity` has
#            federated credentials for the `environment:*` subjects. Restore
#            identity.tf and the provider together.
#   github   removed in phase 5 with github-repo-config.tf. Returns when
#            per-deployment cross-repo distribution does.
#
# kubernetes is declared and used — the namespaces authenticate by Entra token
# through kubelogin, which is why enabling azure_rbac on the cluster mattered.
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_version = ">= 1.9"

  required_providers {
    # Arrives with the namespaces in phase 6. Authenticated by an Entra token via
    # kubelogin rather than a downloaded admin credential — see namespace.tf.
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.0"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    # Added at task 3.5, exactly as promised above: aliased to the personally-
    # owned IDENTITY tenant, used by identity.tf and nothing else.
  }
}
