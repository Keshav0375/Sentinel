# ─────────────────────────────────────────────────────────────────────────────
# AKS — backend hosting (architecture/infra.md §3.7).
#
# Scale-to-zero is the cost model — via `az aks stop`/`start` on the WHOLE
# cluster (backend §8.5), not nodepool scaling: a system pool cannot go below
# 1 node, and a 1-pool cluster's pool is always a system pool. Found live
# 2026-08-23 (SystemPoolSkuTooLow). Terraform provisions the cluster only — the
# sentinel-backend manifests live in the Sentinel repo and are applied by
# ci_backend_deployment.yml, never from here.
#
# This module also owns the backend pod's WORKLOAD IDENTITY: the UAMI the pod
# runs as, federated to the cluster's OIDC issuer. Creating it here (not in a
# separate module) keeps the issuer→credential edge inside one plan.
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

# ── tfsec: two accepted risks ────────────────────────────────────────────────
# azure-container-limit-authorized-ips: the API server is public with no IP
#   allowlist, because it is called from GitHub-hosted runners (the kubernetes
#   provider in the deployment layer, gha-plan's reads) whose egress IPs are
#   not a stable allowlist. Entra-integrated Kubernetes RBAC (azure_rbac_enabled
#   below) gates Entra tokens, and that is NOT the whole surface: local accounts
#   are still ENABLED (local_account_disabled is deferred, see the RBAC block
#   below), so the static cluster-admin client certificate from
#   `az aks get-credentials --admin` bypasses Entra RBAC entirely, and with no
#   IP allowlist that certificate works from anywhere on the internet. Whoever
#   holds it, or can call listClusterAdminCredential (gha-deploy's Cluster Admin
#   Role), is cluster-admin. Accepted by the owner (PR #16 review); disabling
#   local accounts is being raised separately.
# azure-container-logging: Container Insights needs a Log Analytics workspace,
#   billed per GB ingested, against a stack costed at ~$0 idle (infra §11).
#   Datadog is the observability plane for everything Sentinel watches.
#tfsec:ignore:azure-container-limit-authorized-ips
#tfsec:ignore:azure-container-logging
resource "azurerm_kubernetes_cluster" "sentinel" {
  name                = var.cluster_name
  location            = var.location
  resource_group_name = var.resource_group_name
  dns_prefix          = var.dns_prefix

  # Free control plane (no SLA — acceptable for a demo stack; the grant covers it).
  sku_tier = "Free"

  # Both required for workload identity: the cluster publishes an OIDC issuer,
  # and the webhook that projects federated tokens into pods is enabled.
  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  default_node_pool {
    name       = "default"
    node_count = 1
    # B2pls_v2 — ARM64, 2 vCPU / 4 GiB. Third SKU attempt, each rejected live:
    #   B2ats_v2 (grant) → SystemPoolSkuTooLow: system pools need >=4 GB, it has 1
    #   B2s              → not in this subscription's allowed AKS list for
    #                      canadacentral; every allowed small SKU is ARM ('p')
    # B2pls_v2 satisfies the system-pool floor, fits Bpsv2 quota (2 of 10), and
    # bills ~$0.03/hr ONLY while the cluster is started (az aks stop between
    # runs). CONSEQUENCE: the node is arm64 — the backend image must be built
    # linux/arm64 (backend phase 7, docker buildx).
    vm_size = var.node_vm_size

    # 64 GB, not the 128 GB Azure defaults to. The OS disk is billed as a
    # PROVISIONED Premium SSD, so the tier follows the size and not the usage:
    # 128 GB is a P10, 64 GB is a P6, and the P6 is roughly half the price for a
    # disk that holds an OS image and a handful of container layers. Measured on
    # the 2026-08-30 cost report the P10 was 40% of the entire subscription's
    # spend -- second only to the node itself.
    #
    # Ephemeral OS would be free, and is NOT available here: the `l` in
    # B2pls_v2 means the SKU ships no local temp disk, and ephemeral requires
    # one at least as large as the OS disk. Managed is the only option, so the
    # only lever is size.
    #
    # 30 GB is the AKS floor. 64 leaves real headroom for image layers; 32 would
    # save another few dollars and put us one large image away from a node that
    # cannot pull. Not worth it.
    os_disk_size_gb = var.node_os_disk_size_gb

    upgrade_settings {
      max_surge = "10%"
    }
  }

  # ── Entra-integrated Kubernetes RBAC ──────────────────────────────────────
  # This is what makes fetching a kubeconfig SAFE, and it is the reason phase 6
  # could give the plan identity cluster access at all.
  #
  # WITHOUT azure_rbac_enabled, the kubeconfig from `listClusterUserCredential`
  # is effectively cluster-wide access. Handing that to gha-plan — the identity
  # any pull request can trigger — would be the same mistake as granting it
  # `listCredentials`, which phase 5 refused for exactly this reason.
  #
  # WITH it, the kubeconfig carries an exec plugin that mints an Entra token for
  # the CALLING identity, and Azure RBAC decides what that token may do:
  #   gha-plan   → AKS RBAC Reader        (list namespaces, change nothing)
  #   gha-deploy → AKS RBAC Cluster Admin (create namespaces)
  #
  # Future hardening, deliberately not done now: `local_account_disabled = true`
  # removes the admin bypass entirely, but locks out recovery if the Entra path
  # breaks. Worth revisiting once the workflows have run for a while.
  # The provider requires either `tenant_id` or `admin_group_object_ids`.
  # admin_group_object_ids is not an option: creating an Entra security group is
  # a directory write, and the school tenant denies those at policy — the same
  # constraint that killed the `sentinel-db-admins` group in phase 2 (B10).
  # So access is governed purely by Azure RBAC role assignments.
  azure_active_directory_role_based_access_control {
    azure_rbac_enabled = true
    tenant_id          = var.tenant_id
  }

  # ── Network policy that is actually ENFORCED ──────────────────────────────
  # namespace.tf gives every deployment a default-deny-ingress NetworkPolicy.
  # Kubernetes stores that object whether or not anything enforces it; without
  # a policy engine it is inert, and every pod can reach every other pod across
  # namespaces. Cilium is the engine, and it requires Azure CNI with overlay.
  #
  # Cilium rather than Azure NPM or Calico: it runs on the arm64 B2pls_v2
  # node, it is the data plane Azure is converging on, and kubenet (the only
  # thing Calico would pair with here without a VNet) is being retired.
  #
  # Addressing is left to the AKS defaults, and none of them collides: this
  # module sets no VNet, so AKS creates a managed one (10.224.0.0/12); overlay
  # pods take 10.244.0.0/16, services 10.0.0.0/16, kube-dns 10.0.0.10.
  #
  # ForceNew except for some in-place upgrades, so on a live cluster this is a
  # recreate, and a recreate changes the OIDC issuer that every deployment's
  # federated credential trusts. Added while no cluster exists (2026-10-04).
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_data_plane  = "cilium"
    network_policy      = "cilium"
  }

  identity {
    type = "SystemAssigned"
  }

  lifecycle {
    ignore_changes = [default_node_pool[0].node_count]
  }
}

# Kubelet pulls images from ACR with no imagePullSecrets — the grant lives with
# the consumer, which is why it is here and not in the ACR module.
resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                            = var.acr_id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_kubernetes_cluster.sentinel.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}

# The per-deployment backend identity and its federated credential USED to live
# here (phase 3). They moved to the deployment layer in phase 5: the cluster is
# shared and each deployment gets its own namespace, its own UAMI and its own
# federated subject `system:serviceaccount:<namespace>:<sa>`. Binding one
# identity to the cluster module would have made every deployment share it —
# which is precisely the isolation this design exists to provide.
#
# The `gha_aks_user` role assignment also went: gha-deploy now holds
# "Azure Kubernetes Service Cluster Admin Role" at SUBSCRIPTION scope from
# bootstrap-identities.sh, because it must reach a cluster it has not created yet.
