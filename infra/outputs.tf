# ─────────────────────────────────────────────────────────────────────────────
# Root outputs.
#
# Split by layer, because the two are consumed by different readers:
#
#   PLATFORM outputs are read by every deployment, through
#   `data.terraform_remote_state.platform`. They are a contract — renaming one
#   breaks every deployment at once.
#
#   DEPLOYMENT outputs are read by humans and by the other repos' pipelines.
#
# Every output is `try`-guarded on the layer, so the inactive half resolves to
# null rather than failing. `count` on a module makes every reference an index,
# and an unindexed reference in the wrong layer is an error, not an empty value.
#
# Rule kept from phase 4: an output exists because something CONSUMES it. Outputs
# are a public interface; "might be handy" is how a module ends up with thirty
# and no way to tell which are load-bearing.
# ─────────────────────────────────────────────────────────────────────────────

# ── Identity of this workspace ────────────────────────────────────────────────

output "layer" {
  description = "Which layer this workspace manages."
  value       = var.layer
}

output "names" {
  description = "Every name this workspace derives. Exposed for diagnostics — when a global name collides, this is what to compare."
  value       = module.naming.names
}

# ── Platform ──────────────────────────────────────────────────────────────────

output "resource_group_name" {
  description = "The platform resource group."
  value       = try(azurerm_resource_group.platform[0].name, null)
}

# Consumed by: backend CI (image push target) and every runner `container:` block.
output "acr_login_server" {
  description = "ACR login server FQDN. Shared by every deployment — one registry, many images."
  value       = try(module.acr[0].acr_login_server, null)
}

output "acr_id" {
  description = "ACR resource ID, for AcrPull grants in the deployment layer."
  value       = try(module.acr[0].acr_id, null)
}

# Consumed by: `az aks get-credentials`, the pause/resume workflow, and the
# kubernetes provider in the deployment layer.
output "aks_cluster_name" {
  description = "The shared AKS cluster. Deployments get a namespace on it, not a cluster of their own."
  value       = try(module.aks[0].aks_cluster_name, null)
}

output "aks_resource_group" {
  description = "Resource group holding the cluster."
  value       = try(module.aks[0].aks_resource_group, null)
}

# Consumed by: every deployment's federated credential. This is the trust anchor
# for workload identity — it changes if the cluster is recreated, which silently
# invalidates every deployment's credential at once.
output "oidc_issuer_url" {
  description = "AKS OIDC issuer URL — the trust anchor for workload identity."
  value       = try(module.aks[0].oidc_issuer_url, null)
}

# Consumed by: deployments in `database.mode: shared`, and by anyone connecting
# with an Entra token. There is no password to pair with this.
output "postgres_fqdn" {
  description = "Shared Postgres server FQDN. Entra-only auth — no password exists."
  value       = try(module.postgresql[0].db_host, null)
}

# Consumed by: deployments in `database.mode: shared`, which create a database
# on this server without holding the server in their own state — so destroying a
# deployment removes its database and cannot reach the server.
output "postgres_server_id" {
  description = "Resource ID of the shared Postgres server."
  value       = try(module.postgresql[0].server_id, null)
}

output "postgres_admin_principal" {
  description = "The human Entra administrator, so a deployment can grant its own role without re-deriving it."
  value       = try(var.pg_admin_principal_name, null)
}

# ── Deployment ────────────────────────────────────────────────────────────────
# Proves the remote-state seam end to end: these values are READ from the
# platform's state, not computed here. Phase 6 adds the deployment's own
# resources beside them.

output "platform_acr_login_server" {
  description = "The platform's registry, as seen from a deployment workspace. Read-only — a deployment cannot change it."
  value       = try(local.platform.acr_login_server, null)
}

output "platform_aks_cluster_name" {
  description = "The shared cluster, as seen from a deployment workspace."
  value       = try(local.platform.aks_cluster_name, null)
}

output "platform_oidc_issuer_url" {
  description = "The cluster's OIDC issuer, as seen from a deployment workspace. The federated subject for this deployment's namespace is built against it."
  value       = try(local.platform.oidc_issuer_url, null)
}

# ── What the app pipeline needs (decision 2026-10-05, R11) ───────────────────
# Read by scripts/push-deploy-config.sh, which pushes them ONCE to the
# `sentinel-dev` environment on Sentinel-deployment (R8). They are deterministic
# (uid = sha1(sub-dep-env)[0:4]), so a destroy/recreate of the same deployment
# reproduces them and the push need not be repeated. Null when the component is
# off, and the push refuses rather than writing an empty variable.

# Consumed by: R8 push -> APP_NAME (azure/webapps-deploy target).
output "app_name" {
  description = "The target web app's name in this deployment."
  value       = try(module.app_service[0].app_name, null)
}

# Consumed by: R8 push -> DEPLOYED_APP_URL (the pipeline's post-deploy health check).
output "app_url" {
  description = "Public https URL of the target web app."
  value       = try(module.app_service[0].app_url, null)
}

# Consumed by: R8 push -> AZURE_RG (the resource group the pipeline deploys into).
output "deployment_resource_group" {
  description = "This deployment's primary resource group (App Service, vault, dedicated database)."
  value       = try(azurerm_resource_group.deployment[0].name, null)
}

# Consumed by: R8 push -> PG_DATABASE, and scripts/grant-db-access.sh (R12).
# Whichever mode is active; both name the database `<deployment>_<env>`.
output "database_name" {
  description = "This deployment's database, on the shared platform server or its own dedicated one."
  value = try(
    azurerm_postgresql_flexible_server_database.shared[0].name,
    module.database[0].db_name,
    null,
  )
}

# Consumed by: R8 push -> PG_HOST, and scripts/grant-db-access.sh (R12).
# Not one of R11's four: the push needs the server as well as the database, and
# in `shared` mode it lives in the PLATFORM's state, so it is resolved here
# rather than by a script switching workspaces.
output "database_host" {
  description = "FQDN of the server holding database_name — the platform server in `shared` mode, this deployment's own in `dedicated`."
  value = try(
    module.database[0].db_host,
    local.c_db_shared == 1 ? local.platform.postgres_fqdn : null,
    null,
  )
}

# ── What the Datadog webhook needs (decision 2026-10-05 "Deploy phase 2") ────
# Not secret. The topic KEY is deliberately absent: it would land in every
# consumer's output, so apply.sh reads it live with `az eventgrid topic key
# list --name <event_grid_topic_name> -g <deployment_resource_group>`.

# Consumed by: Sentinel-deployment datadog/apply.sh (the topic-key lookup).
output "event_grid_topic_name" {
  description = "This deployment's Event Grid topic — the Datadog webhook's target."
  value       = try(module.event_grid[0].topic_name, null)
}

# Consumed by: Sentinel-deployment datadog/apply.sh (the webhook URL).
output "event_grid_endpoint" {
  description = "Ingest URL of this deployment's Event Grid topic; the Datadog webhook posts here."
  value       = try(module.event_grid[0].topic_endpoint, null)
}
