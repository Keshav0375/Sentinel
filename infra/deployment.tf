# ─────────────────────────────────────────────────────────────────────────────
# The DEPLOYMENT layer — one isolated deployment, built against the platform.
#
# ── Why a read-only remote-state lookup and not variables ────────────────────
# A deployment NEEDS the cluster's OIDC issuer and the registry's login server,
# and must be unable to change either. A data source enforces exactly that.
# Passing them as variables would mean the same values written in two places,
# drifting the first time one is edited.
#
# It also enforces the direction of dependency. Platform does not know its
# deployments exist; deployments cannot write platform state. Destroying a
# deployment therefore CANNOT touch the cluster — not by convention, but because
# its state file contains none of it.
#
# Everything here is `count`-gated on the layer, so the platform workspace plans
# zero of it. Kubernetes resources live in namespace.tf (task 6.2); this file is
# the Azure half.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  deployment_count = local.is_deployment ? 1 : 0
}

data "terraform_remote_state" "platform" {
  count = local.deployment_count

  backend = "azurerm"

  # The `workspace` ARGUMENT, not a hand-built key. The azurerm backend stores a
  # workspace's state at `<key>env:<workspace>`, appending rather than nesting —
  # `env:/platform/sentinel.tfstate` is the S3 backend's layout and would read a
  # blob that does not exist. Getting this wrong produces "no state found",
  # which reads like the platform was never applied.
  workspace = "platform"

  config = {
    resource_group_name  = "rg-sentinel-bootstrap"
    storage_account_name = var.state_storage_account
    container_name       = "tfstate"
    key                  = "sentinel.tfstate"
    use_azuread_auth     = true
    use_oidc             = true
  }
}

locals {
  # Empty at the platform layer, so nothing here can be referenced by accident.
  platform = try(data.terraform_remote_state.platform[0].outputs, {})

  # ── Component toggles (task 6.4) ───────────────────────────────────────────
  # `components` from the config drives `count` on each module. These are NOT
  # independent — the dependency graph is enforced by preconditions below,
  # because an invalid combination should fail in seconds with a sentence rather
  # than four hundred lines into a plan with a provider error about an attribute.
  components = try(local.cfg.components, [])

  want_keyvault    = local.is_deployment && contains(local.components, "keyvault")
  want_database    = local.is_deployment && contains(local.components, "database")
  want_functions   = local.is_deployment && contains(local.components, "functions")
  want_event_grid  = local.is_deployment && contains(local.components, "event_grid")
  want_app_service = local.is_deployment && contains(local.components, "app_service")
  want_namespace   = local.is_deployment && contains(local.components, "namespace")

  c_keyvault    = local.want_keyvault ? 1 : 0
  c_database    = local.want_database ? 1 : 0
  c_functions   = local.want_functions ? 1 : 0
  c_event_grid  = local.want_event_grid ? 1 : 0
  c_app_service = local.want_app_service ? 1 : 0
  c_namespace   = local.want_namespace ? 1 : 0

  # The backend identity exists only when there is something for it to reach.
  c_backend_identity = (local.want_namespace && local.want_keyvault) ? 1 : 0
}

# Fails fast and legibly when a deployment is planned before the platform exists.
# Without it the first error is a missing map key several modules deep, which
# names an attribute rather than the cause.
resource "terraform_data" "platform_available" {
  count = local.deployment_count

  lifecycle {
    precondition {
      condition     = try(local.platform.aks_cluster_name, "") != ""
      error_message = "platform state is empty or unreadable. Apply the platform layer first: terraform workspace select platform && terraform apply -var layer=platform"
    }
  }
}

# ── Component dependency graph (task 6.4) ─────────────────────────────────────
# Checkbox independence is a lie, and this resource exists to say so out loud.
# Unchecking `keyvault` while `functions` is on does not produce a smaller stack;
# it produces a provider error hundreds of lines into a plan, naming a resource
# attribute rather than the mistake.
resource "terraform_data" "component_graph" {
  count = local.deployment_count

  lifecycle {
    # The bridge reads GITHUB_TOKEN as a Key Vault reference (§3.5), and the
    # rotator's whole purpose is rotating vault secrets.
    precondition {
      condition     = !local.want_functions || local.want_keyvault
      error_message = "components: `functions` requires `keyvault` — the bridge reads GITHUB_TOKEN as a Key Vault reference and the rotator manages vault secrets."
    }

    # These two are a single unit, not two things that happen to pair. The Event
    # Grid subscription needs the function app id, and the bridge is only
    # reachable through that subscription.
    precondition {
      condition     = local.want_functions == local.want_event_grid
      error_message = "components: `functions` and `event_grid` are mutually dependent — the subscription needs the function app id, and the bridge is only reachable through the subscription. Enable both or neither."
    }

    # A backend pod with no database and no vault has nothing to do.
    precondition {
      condition     = !local.want_namespace || (local.want_database && local.want_keyvault)
      error_message = "components: `namespace` requires `database` and `keyvault` — the backend identity is granted on both, and a pod without them has nothing to reach."
    }

    # The shared server lives in the platform layer; a deployment cannot create
    # a database on a server that does not exist.
    precondition {
      condition     = !local.want_database || try(local.platform.postgres_fqdn, "") != "" || try(local.cfg.database.mode, "shared") == "dedicated"
      error_message = "components: `database` in `shared` mode needs the platform's Postgres server. Apply the platform layer, or set database.mode = dedicated in azure/config/deployment-config.yaml."
    }
  }
}

# ── Resource groups ───────────────────────────────────────────────────────────
# MANAGED, not read-only data sources. C1 made them bootstrap-created so that
# `terraform destroy` could not delete them — right when there was one permanent
# estate, wrong when deployments come and go on demand.
resource "azurerm_resource_group" "deployment" {
  count    = local.deployment_count
  name     = module.naming.names.resource_group
  location = local.location

  tags = {
    layer      = "deployment"
    deployment = var.deployment
    env        = var.environment
    managed_by = "terraform"
  }
}

# Linux Consumption (Y1) and Linux Dedicated (F1) plans CANNOT share a resource
# group — Azure rejects the second with "Requested features 'Dynamic SKU, Linux
# Worker' not available in resource group". Found live 2026-08-23; it is a
# property of Azure's webspace allocation, not of our layout, so every
# deployment needs two groups.
resource "azurerm_resource_group" "functions" {
  count    = local.c_functions
  name     = module.naming.names.functions_resource_group
  location = local.location

  tags = {
    layer      = "deployment"
    deployment = var.deployment
    env        = var.environment
    managed_by = "terraform"
  }
}

# ── Backend workload identity ─────────────────────────────────────────────────
# The Azure half of the pair. Its federated credential is here too, because the
# subject is a STRING from the naming module — it does not need the Kubernetes
# namespace resource to exist, only its name. The ServiceAccount that completes
# the pair is in namespace.tf.
#
# This is where per-deployment isolation actually lives. The subject is
# `system:serviceaccount:<d>-<env>:sentinel-backend`, Entra matches it as an
# exact string with no wildcard form, so a pod in one namespace cannot obtain a
# token for another deployment's vault or database.
resource "azurerm_user_assigned_identity" "backend" {
  count               = local.c_backend_identity
  name                = module.naming.names.backend_identity
  resource_group_name = azurerm_resource_group.deployment[0].name
  location            = local.location
}

resource "azurerm_federated_identity_credential" "backend" {
  count = local.c_backend_identity
  name  = "${module.naming.names.kubernetes_namespace}-backend"
  # azurerm 4.81 deprecates `resource_group_name` (unused) and renames
  # `parent_id` to this. Both old arguments are Optional+Computed and Read sets
  # all three from the resource ID, so the switch plans as no change.
  user_assigned_identity_id = azurerm_user_assigned_identity.backend[0].id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = local.platform.oidc_issuer_url
  subject                   = "system:serviceaccount:${module.naming.names.kubernetes_namespace}:sentinel-backend"
}

# ── Key Vault ─────────────────────────────────────────────────────────────────
# Created EMPTY, and that is not an oversight. Terraform ships zero
# `azurerm_key_vault_secret` resources: a secret written by Terraform is a secret
# in state, readable by anything with state access and present in every plan.
# Seeding is a documented `az keyvault secret set` step per deployment.
#
# A deployment with an unseeded vault is a VALID state — the bridge's Key Vault
# reference resolves to the literal `@Microsoft.KeyVault(...)` string, which the
# handlers detect and log rather than crashing on (phase 3, with a unit test).
module "keyvault" {
  source = "./modules/keyvault"
  count  = local.c_keyvault

  resource_group_name = azurerm_resource_group.deployment[0].name
  location            = local.location
  vault_name          = module.naming.names.key_vault

  # Pinned from the root, never data.azurerm_client_config — under CI that
  # resolves to the pipeline identity and silently strips the human's access.
  tenant_id          = var.tenant_id
  kv_admin_object_id = var.kv_admin_object_id

  # Additional to kv_admin, never instead of it — the human keeps Officer.
  seeder_principal_id = var.kv_seeder_object_id

  enable_bridge_reader   = local.want_functions
  bridge_principal_id    = try(module.functions[0].bridge_principal_id, null)
  enable_rotator_officer = local.want_functions
  rotator_principal_id   = try(module.functions[0].rotator_principal_id, null)
  rotator_app_id         = try(module.functions[0].rotator_app_id, null)

  enable_backend_reader     = local.c_backend_identity == 1
  backend_uami_principal_id = try(azurerm_user_assigned_identity.backend[0].principal_id, null)
}

# ── Functions: the Event Grid → GitHub bridge, and the secret rotator ─────────
# `key_vault_name` comes from the NAMING module, not from module.keyvault, and
# that is load-bearing: the vault grants the bridge access (it needs
# bridge_principal_id) while the bridge references the vault by name. Taking the
# name from the module would make that a dependency cycle Terraform refuses to
# plan. A name is just a string; only the grant is a real edge.
module "functions" {
  source = "./modules/functions"
  count  = local.c_functions

  resource_group_name  = azurerm_resource_group.functions[0].name
  location             = local.location
  storage_account_name = module.naming.names.storage_account
  bridge_name          = module.naming.names.function_app
  rotator_name         = "${module.naming.names.function_app}-rot"
  key_vault_name       = module.naming.names.key_vault
}

# ── Event Grid ────────────────────────────────────────────────────────────────
# Datadog monitors POST here; the subscription routes to the bridge, which stamps
# `signal_type` and dispatches to GitHub.
module "event_grid" {
  source = "./modules/event-grid"
  count  = local.c_event_grid

  resource_group_name = azurerm_resource_group.deployment[0].name
  location            = local.location
  topic_name          = module.naming.names.event_grid_topic
  function_app_id     = module.functions[0].function_app_id
}

# ── Target application ────────────────────────────────────────────────────────
# F1: `always_on` is unavailable and 32-bit workers are forced. Both are
# properties of the free tier, not choices.
module "app_service" {
  source = "./modules/app-service"
  count  = local.c_app_service

  resource_group_name = azurerm_resource_group.deployment[0].name
  location            = local.location
  app_name            = module.naming.names.app_service
}

# ── gha-app may deploy code to THIS app and nothing else ─────────────────────
# Decision 2026-10-05 (R7). gha-app is the Sentinel-deployment pipeline's
# identity, created by scripts/bootstrap-identities.sh with no role at all. This
# is its only Azure grant: Website Contributor scoped to one web app, so a
# compromised app pipeline can redeploy or restart the target and cannot touch
# the plan, the resource group, or anything else in the subscription.
#
# Gated three ways: the component, the variable (an unset GHA_APP_OBJECT_ID
# repository variable must plan a deployment, not fail it), and the target.
# Only the deployment gha-app's single federated environment points at
# (var.gha_app_target, `sentinel-dev`) gets the grant; any other deployment's
# app stays out of the pipeline identity's reach.
resource "azurerm_role_assignment" "gha_app_website_contributor" {
  count = (
    local.want_app_service
    && var.gha_app_object_id != null && var.gha_app_object_id != ""
    && "${var.deployment}-${var.environment}" == var.gha_app_target
  ) ? 1 : 0

  scope                = module.app_service[0].app_id
  role_definition_name = "Website Contributor"
  principal_id         = var.gha_app_object_id

  # Same flag and reason as the vault grants in modules/keyvault: no directory
  # lookup against a service principal Terraform did not create. Create-only —
  # it fails with "doesn't support update" if added to an existing assignment.
  skip_service_principal_aad_check = true
}
