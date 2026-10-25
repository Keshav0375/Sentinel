# ─────────────────────────────────────────────────────────────────────────────
# PostgreSQL Flexible Server (architecture/infra.md §3.2).
#
# Entra-ONLY authentication. There is no administrator password anywhere in this
# module, in state, or in Key Vault — `password_auth_enabled = false` means one
# cannot exist. Every client presents a short-lived Entra access token (audience
# https://ossrdbms-aad.database.windows.net) as the psql password instead.
#
# B1MS + 32 GB storage matches the Azure for Students 12-month free grant exactly
# (750 h/month of Burstable B1MS, 32 GB storage, 32 GB backup). Note the module
# does NOT configure backup — backup_retention_days is left at the Azure default
# of 7 days; the 32 GB is the grant's allowance, not a setting here. Changing the
# SKU or raising storage takes this off the free meter.
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

resource "azurerm_postgresql_flexible_server" "sentinel" {
  name                = var.server_name
  resource_group_name = var.resource_group_name
  location            = var.location

  version    = "16"
  sku_name   = var.sku_name
  storage_mb = var.storage_mb
  zone       = "1"

  # No administrator_login / administrator_password. Entra-only (rev-5): a stolen
  # connection string is useless without a live token, and there is no long-lived
  # credential to rotate, leak, or forget to rotate.
  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false

    # Azure populates this server-side the moment an Entra administrator is
    # attached. Omitting it gives a PERPETUAL DIFF: every plan proposes setting
    # tenant_id back to null, which would read as drift forever and train the
    # reader to ignore a non-empty plan.
    tenant_id = var.tenant_id
  }
}

# ── Entra administrators ──────────────────────────────────────────────────────
# rev-9: the `sentinel-db-admins` group is gone — creating a group is a directory
# write the uwindsor.ca tenant denies. Admins attach directly instead, one
# resource per principal. Only an Entra admin can create further DB roles via
# pgaadauth_create_principal (§10 step 8), which is how sentinel-gha and the
# backend get their non-admin logins.

# The human owner — break-glass psql access, and the principal that runs
# §10 step 8 to create the other roles.
resource "azurerm_postgresql_flexible_server_active_directory_administrator" "human" {
  server_name         = azurerm_postgresql_flexible_server.sentinel.name
  resource_group_name = var.resource_group_name
  tenant_id           = var.tenant_id
  object_id           = var.postgres_entra_admin_object_id
  principal_name      = var.postgres_entra_admin_principal_name
  principal_type      = "User"

  # Entra auth must be ON before any Entra admin can be attached.
  depends_on = [azurerm_postgresql_flexible_server.sentinel]
}

# The backend pod's workload identity. Deferred: the UAMI is created by the AKS
# module in phase 3 task 3.1, so there is nothing to point at yet. Phase 3 flips
# enable_backend_admin and this resource appears — no edit to this file.
#
# The guard is a bool rather than a null-check on the principal id itself,
# because `count` must be resolvable at PLAN time and the phase-3 caller will
# pass an id that is only known after apply. Task 2.3 guards the same way.
resource "azurerm_postgresql_flexible_server_active_directory_administrator" "backend_uami" {
  count = var.enable_backend_admin ? 1 : 0

  server_name         = azurerm_postgresql_flexible_server.sentinel.name
  resource_group_name = var.resource_group_name
  tenant_id           = var.tenant_id
  object_id           = var.backend_uami_principal_id
  principal_name      = var.backend_uami_principal_name
  principal_type      = "ServicePrincipal"

  depends_on = [azurerm_postgresql_flexible_server.sentinel]
}

# ── Database ──────────────────────────────────────────────────────────────────
#
# ── Why this depends on the administrators, and why that is about DESTROY ─────
# Nothing here needs an admin to exist in order to create a database — the ARM
# control plane creates it. The dependency is declared for the order Terraform
# derives from it in REVERSE: on destroy, dependents die first, so the databases
# are dropped before the Entra principals that own them.
#
# Without it the two are independent, Terraform destroys them concurrently, and
# the admin drop loses the race. Azure runs `DROP ROLE` inside Postgres, which
# refuses:
#
#   AadAuthPrincipalDropFailed: Failed to drop Microsoft Entra principal.
#   Reason: '2BP01: role "<upn>" cannot be dropped because some objects
#   depend on it'
#
# 2BP01 is `dependent_objects_still_exist`. The admin created the database, so
# it owns it, and a role that owns objects cannot be dropped.
#
# The consequence is worse than one failed resource. That delete leaves a Failed
# terminal state on the server, every other server-scoped operation queues behind
# it, and Terraform polls them for half an hour until the GitHub OIDC assertion
# request times out —
#
#   githubAssertion: cannot request token: ... context deadline exceeded
#
# — an error that names neither Postgres nor the role. Seen live in run
# 34779806599: 29m51s of "Still destroying", the server itself never attempted,
# and rg-sentinel-plat-cc left standing and billing.
#
# A deployment's database (database.tf, `shared` mode) is owned by the same
# admin and lives in a DIFFERENT workspace, so no Terraform edge can express it.
# scripts/lifecycle.sh covers that case by destroying every deployment before
# the platform, which is the same ordering property enforced one level up.
resource "azurerm_postgresql_flexible_server_database" "sentinel" {
  name      = var.database_name
  server_id = azurerm_postgresql_flexible_server.sentinel.id
  charset   = "UTF8"
  collation = "en_US.utf8"

  depends_on = [
    azurerm_postgresql_flexible_server_active_directory_administrator.human,
    azurerm_postgresql_flexible_server_active_directory_administrator.backend_uami,
  ]
}

# pgvector must be allow-listed at the server before the backend can run
# `CREATE EXTENSION vector` (alembic migration 001). Allow-listing does not
# create the extension — it only permits it.
resource "azurerm_postgresql_flexible_server_configuration" "pgvector" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.sentinel.id
  value     = "VECTOR"
}

# ── Firewall ──────────────────────────────────────────────────────────────────
# Allow-all is deliberate for dev, and safe here for one specific reason
# (§3.2 "Network reach ≠ auth"): password auth is DISABLED on this server, so
# reaching the port gets an attacker nothing without a valid Entra token issued
# to an admin or a pgaadauth-created role. The firewall controls who can *reach*
# the port, not who can *log in*.
#
# Two client classes need reach and neither has a stable IP: GitHub-hosted
# runners (ci_app_deployment records deploys, ci_incident_response fetches
# context) rotate across a wide published range, and the AKS node pool has no
# fixed egress IP without a NAT gateway. Production would use a Private Endpoint
# + VNet integration; that is out of scope for a demo stack and would break the
# GitHub-hosted runners entirely.
#
# LIMIT OF THAT ARGUMENT: it is about who can LOG IN, not about keeping the
# server UP. max_connections on B_Standard_B1ms is 50, and connection slots are
# consumed before authentication completes — so a trivial connection flood from
# anywhere on IPv4 can exhaust them and deny service to the backend pod and every
# CI workflow, without bypassing auth at all. Accepted for a demo stack on a
# burstable SKU; a Private Endpoint is the real fix and is out of scope.
#
# tfsec/tflint will flag this rule. Annotate the finding — never silence it.
resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_all_dev" {
  name             = "allow-all-dev"
  server_id        = azurerm_postgresql_flexible_server.sentinel.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "255.255.255.255"
}
