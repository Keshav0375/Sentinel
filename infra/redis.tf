# ─────────────────────────────────────────────────────────────────────────────
# The deployment's short-term memory: Azure Managed Redis.  (decision 2026-10-25 D)
#
# Balanced B0, single node, Entra-only, reachable only through a private
# endpoint in the platform's AKS subnet — see modules/managed-redis for why each.
# It holds no durable state, so it is created and destroyed with the deployment;
# the durable half of agent memory is in Postgres and is backed up before a
# destroy (scripts/memory-backup.sh).
#
# `count` needs both the `redis` component and the backend identity: the
# identity's access policy is the only way in, so a cache without it would be
# unreachable by design.
# ─────────────────────────────────────────────────────────────────────────────

module "redis" {
  source = "./modules/managed-redis"
  count  = local.c_redis

  resource_group_name  = azurerm_resource_group.deployment[0].name
  location             = local.location
  redis_name           = module.naming.names.managed_redis
  subnet_id            = local.platform.aks_subnet_id
  private_dns_zone_id  = local.platform.redis_private_dns_zone_id
  backend_principal_id = azurerm_user_assigned_identity.backend[0].principal_id

  tags = {
    layer      = "deployment"
    deployment = var.deployment
    env        = var.environment
    managed_by = "terraform"
  }
}
