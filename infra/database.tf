# ─────────────────────────────────────────────────────────────────────────────
# The deployment's database, in one of two modes.  (task 6.3)
#
#   shared     a database on the PLATFORM server. One server's compute serves
#              every deployment. Cheap. Accepted cost: shared compute, so a
#              runaway query in one deployment can slow another.
#
#   dedicated  its own flexible server in this deployment's resource group.
#              Full isolation of compute, storage, admins and backups — and a
#              whole VM against the same grant, ~8 minutes to create, plus one
#              more server that must be RUNNING before any plan can refresh it.
#
# ── Both modes expose the same contract ──────────────────────────────────────
# `db_host` / `db_name`, Entra-only, no password in either case. The backend must
# not be able to tell which mode it got — otherwise `mode` stops being a
# deployment-time knob and becomes an application concern.
#
# ── What Terraform does NOT do here ──────────────────────────────────────────
# It does not create the Entra ROLE inside the database. That is
# `pgaadauth_create_principal`, a psql-level call with no Terraform resource
# (§3.2). Terraform creates the database and attaches server-level Entra
# administrators; granting the workload identity access INSIDE the database is a
# documented step. Pretending otherwise would leave a deployment that looks
# complete and cannot connect.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  db_mode = try(local.cfg.database.mode, "shared")

  c_db_shared    = (local.want_database && local.db_mode == "shared") ? 1 : 0
  c_db_dedicated = (local.want_database && local.db_mode == "dedicated") ? 1 : 0
}

resource "terraform_data" "database_mode_valid" {
  count = local.c_database

  lifecycle {
    precondition {
      condition     = contains(["shared", "dedicated"], local.db_mode)
      error_message = "database.mode must be `shared` or `dedicated`, got `${local.db_mode}`. Set it in azure/config/deployment-config.yaml."
    }
  }
}

# ── shared ────────────────────────────────────────────────────────────────────
# A database on the platform's server, addressed by the server ID the platform
# published. The deployment cannot modify the server itself — it has no such
# resource in state, only this child.
resource "azurerm_postgresql_flexible_server_database" "shared" {
  count = local.c_db_shared

  name      = replace("${var.deployment}_${var.environment}", "-", "_")
  server_id = local.platform.postgres_server_id
  charset   = "UTF8"

  # en_US.utf8 rather than the default: it is what the platform server uses, and
  # a collation mismatch between databases on one server produces sort-order
  # differences that surface as inexplicable test failures.
  collation = "en_US.utf8"

  # Azure refuses to change collation or charset in place, so any edit here is a
  # destroy-and-recreate — which for a database means data loss. Explicit rather
  # than discovered.
  lifecycle {
    prevent_destroy = false
  }
}

# ── dedicated ─────────────────────────────────────────────────────────────────
# The same module the platform uses, pointed at this deployment's resource group.
# Reusing it rather than writing a second Postgres implementation is what keeps
# the two modes' contracts identical — Entra-only auth, pgvector, the firewall
# rule, all of it is one code path.
module "database" {
  source = "./modules/postgresql"
  count  = local.c_db_dedicated

  resource_group_name = azurerm_resource_group.deployment[0].name
  location            = local.location
  server_name         = module.naming.names.postgres_server

  tenant_id                           = var.tenant_id
  postgres_entra_admin_object_id      = var.pg_admin_object_id
  postgres_entra_admin_principal_name = var.pg_admin_principal_name

  database_name = replace("${var.deployment}_${var.environment}", "-", "_")
  sku_name      = try(local.cfg.database.sku, "B_Standard_B1ms")
  storage_mb    = try(local.cfg.database.storage_mb, 32768)

  # The deployment's own workload identity becomes the second Entra admin, so
  # the backend pod can reach its database without a password.
  enable_backend_admin        = local.c_backend_identity == 1
  backend_uami_principal_id   = try(azurerm_user_assigned_identity.backend[0].principal_id, null)
  backend_uami_principal_name = module.naming.names.backend_identity
}
