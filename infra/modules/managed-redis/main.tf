# ─────────────────────────────────────────────────────────────────────────────
# Azure Managed Redis — the backend's short-term memory (decision 2026-10-25 D).
#
# Holds nothing durable: the short-term working-memory store, the hot front of
# the tool / LLM cache, and the status-event pub/sub. Checkpoints and the cache
# of record live in Postgres. Losing it costs a cache warm-up, never a record,
# which is what makes the choices below acceptable.
#
#   Balanced_B0, HA off   ~$13.14/month for a single node (docs/COSTS.md). A
#                         replica would double that to protect data Postgres
#                         already holds.
#   Entra only            `access_keys_authentication_enabled = false`, so no
#                         access key exists to leak; the backend's workload
#                         identity is the one principal granted, by the access
#                         policy assignment below.
#   EnterpriseCluster     one endpoint, with sharding handled server-side, so the
#                         backend uses a plain redis.asyncio.Redis client rather
#                         than a cluster-aware one.
#   VolatileLRU           only keys carrying a TTL are evicted under memory
#                         pressure; the backend TTLs every cache key.
#   TLS on port 10000     `client_protocol = "Encrypted"`; the port is the
#                         default database's, read back as an output.
#
# ── Why the private endpoint joins the AKS-managed subnet ────────────────────
# Public network access is disabled, so the cache is reachable only through a
# private endpoint. There is no BYO VNet in this estate: AKS runs in the managed
# VNet it creates in its node resource group, and `aks-subnet` there is the only
# subnet the pods' traffic leaves from. The platform layer publishes that
# subnet's ID and the privatelink DNS zone; this module only consumes them.
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  # Declared per-module rather than inherited, as in modules/postgresql.
  required_version = ">= 1.9"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

resource "azurerm_managed_redis" "this" {
  name                = var.redis_name
  resource_group_name = var.resource_group_name
  location            = var.location

  sku_name                  = "Balanced_B0"
  high_availability_enabled = false
  public_network_access     = "Disabled"

  default_database {
    access_keys_authentication_enabled = false
    client_protocol                    = "Encrypted"
    clustering_policy                  = "EnterpriseCluster"
    eviction_policy                    = "VolatileLRU"
  }

  tags = var.tags
}

# The backend's workload identity, and nothing else. Without an assignment an
# Entra token authenticates and then holds no data access at all.
resource "azurerm_managed_redis_access_policy_assignment" "backend" {
  managed_redis_id = azurerm_managed_redis.this.id
  object_id        = var.backend_principal_id
}

resource "azurerm_private_endpoint" "redis" {
  name                = "pe-${var.redis_name}"
  resource_group_name = var.resource_group_name
  location            = var.location
  subnet_id           = var.subnet_id

  private_service_connection {
    name                           = "psc-${var.redis_name}"
    private_connection_resource_id = azurerm_managed_redis.this.id
    subresource_names              = ["redisEnterprise"]
    is_manual_connection           = false
  }

  # Registers the A record in the platform's privatelink.redis.azure.net zone,
  # which is linked to the AKS VNet, so the public hostname resolves to the
  # private IP from inside the cluster.
  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [var.private_dns_zone_id]
  }

  tags = var.tags
}
