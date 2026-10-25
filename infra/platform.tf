# ─────────────────────────────────────────────────────────────────────────────
# The PLATFORM layer — what every deployment shares.
#
# One resource group, one registry, ONE cluster, one database server. Built once,
# in its own workspace, and read by every deployment through a read-only
# remote-state lookup.
#
# ── Why one cluster and not one per deployment ───────────────────────────────
# The subscription's regional quota is 6 vCPU and an AKS node is 2. Cluster per
# deployment therefore stops at three, with no headroom for a second node
# anywhere. A namespace per deployment scales to whatever the cluster's RAM
# holds.
#
# The isolation is not weaker for being logical. Workload identity federates on
# `system:serviceaccount:<namespace>:<sa>`, and Entra matches that subject as an
# exact string with no wildcard form — so a pod in one namespace cannot obtain a
# token for another's Key Vault. That separation is cryptographic, not a naming
# convention. (The `ResourceQuota` that stops one namespace starving the others
# is a deployment-layer concern; phase 6, task 6.2.)
#
# Everything here is `count`-gated on the layer, so a deployment workspace plans
# ZERO of it.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  platform_count = local.is_platform ? 1 : 0
}

# MANAGED, not a read-only data source. C1 made resource groups bootstrap-created
# and read-only so that `terraform destroy` could not delete them — sensible when
# there was one permanent estate. Under a model where deployments are created and
# destroyed on demand it would mean a manual step per deployment, which is the
# whole thing this phase removes. Only rg-sentinel-bootstrap stays out-of-band,
# because state cannot delete itself.
resource "azurerm_resource_group" "platform" {
  count    = local.platform_count
  name     = module.naming.names.resource_group
  location = local.location

  tags = {
    layer      = "platform"
    managed_by = "terraform"
  }
}

# ── Container registry ────────────────────────────────────────────────────────
# One registry for every deployment's images. Registries are cheap and the free
# grant covers exactly one Standard.
#
# Standard, NOT Basic. The AzureForStudents grant covers 1 Standard registry;
# Basic is a separate billable meter at roughly $5/month. Cheaper and free are
# different questions on a subsidised subscription — this was got wrong once.
module "acr" {
  source = "./modules/acr"
  count  = local.platform_count

  resource_group_name = azurerm_resource_group.platform[0].name
  location            = local.location
  registry_name       = module.naming.names.container_registry
}

# ── Kubernetes ────────────────────────────────────────────────────────────────
# ARM64 (`Standard_B2pls_v2`), and that constraint propagates: backend images
# must build `linux/arm64`. It is not a preference — the grant SKU `B2ats_v2`
# fails the system pool's 4 GB RAM floor with `SystemPoolSkuTooLow`, `B2s` is not
# in this subscription's allowed AKS list for canadacentral, and every permitted
# small SKU there is ARM.
module "aks" {
  source = "./modules/aks"
  count  = local.platform_count

  resource_group_name = azurerm_resource_group.platform[0].name
  location            = local.location
  cluster_name        = module.naming.names.aks_cluster
  dns_prefix          = module.naming.names.aks_dns_prefix
  node_vm_size        = try(local.cfg.aks.vm_size, "Standard_B2pls_v2")
  tenant_id           = var.tenant_id
  acr_id              = module.acr[0].acr_id
}

# ── Database ──────────────────────────────────────────────────────────────────
# One flexible server. Deployments in `database.mode: shared` get a database and
# an Entra role here; `dedicated` builds its own server in its own resource group
# (phase 6, task 6.3). A server is a VM, so one per deployment is real money
# against the same grant.
#
# Entra-only: `password_auth_enabled = false`. There is no password anywhere in
# this repo and adding one would be a defect, not a convenience. Callers mint a
# token with `az account get-access-token --resource-type oss-rdbms`.
module "postgresql" {
  source = "./modules/postgresql"
  count  = local.platform_count

  resource_group_name = azurerm_resource_group.platform[0].name
  location            = local.location
  server_name         = module.naming.names.postgres_server

  # var.tenant_id, never data.azurerm_client_config — this attribute is ForceNew,
  # so an ambient context that repointed would propose destroying the server.
  tenant_id = var.tenant_id

  postgres_entra_admin_object_id      = var.pg_admin_object_id
  postgres_entra_admin_principal_name = var.pg_admin_principal_name
}
