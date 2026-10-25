# ─────────────────────────────────────────────────────────────────────────────
# The deployment's slice of the SHARED cluster.  (task 6.2)
#
# ── Why a namespace and not a cluster ────────────────────────────────────────
# The subscription's regional quota is 6 vCPU and an AKS node is 2, so
# cluster-per-deployment stops at three with no headroom for a second node
# anywhere. A namespace per deployment scales to whatever the cluster's RAM
# holds.
#
# The isolation is not weaker for being logical. Workload identity federates on
#
#     system:serviceaccount:<deployment>-<env>:sentinel-backend
#
# and Entra matches that subject as an exact string with NO wildcard form. A pod
# in one namespace presenting its token cannot obtain another deployment's Key
# Vault or database access — the separation is cryptographic, not a naming
# convention. The Azure half of that pair (the UAMI and its federated credential)
# is in deployment.tf; this file is the Kubernetes half.
#
# ── Why the ResourceQuota is mandatory ───────────────────────────────────────
# With one shared cluster, an unbounded namespace can exhaust the node and take
# every other deployment down with it. The quota is what makes "shared cluster"
# acceptable rather than merely cheap.
#
# The LimitRange beside it is not decoration: a ResourceQuota that sets
# `requests.cpu` REJECTS any pod that does not declare its own requests, with an
# admission error about quota rather than about the manifest. The LimitRange
# supplies defaults so a normal Deployment still schedules.
# ─────────────────────────────────────────────────────────────────────────────

# Read-only. The cluster belongs to the platform layer and a deployment must be
# unable to change it — which is also why this is a data source rather than
# anything reached through the remote state's outputs.
data "azurerm_kubernetes_cluster" "platform" {
  count               = local.c_namespace
  name                = local.platform.aks_cluster_name
  resource_group_name = local.platform.aks_resource_group
}

locals {
  kube = try(data.azurerm_kubernetes_cluster.platform[0].kube_config[0], null)
}

# ── Provider auth: an Entra token, not a downloaded admin credential ─────────
# Provider blocks cannot be conditional, so every value is `try`-guarded and
# resolves to "" at the platform layer, where no cluster data is read.
#
# `exec` with kubelogin is the whole point of enabling `azure_rbac_enabled` on
# the cluster. The kubeconfig carries no static credential; kubelogin mints an
# Entra token for whichever identity is running, and Azure RBAC decides what that
# token may do — Reader for gha-plan, Cluster Admin for gha-deploy. Without
# azure_rbac_enabled this same kubeconfig would BE cluster-wide access, which is
# why phase 5 refused to hand it to a PR-reachable identity.
#
# ⚠️ kubelogin must be on PATH. GitHub runners do not ship it; the workflows add
# `azure/use-kubelogin@v1`.
#
# ⚠️ `--login azurecli` borrows the az CLI's session, and in CI that session
# rests on a GitHub OIDC assertion that expires within MINUTES of azure/login.
# The first namespace call asks for a token for a resource az has not minted
# one for yet (the AKS server app below), so it needs a live assertion. On
# 2026-10-06 (run 37399746578) the platform apply took ~10 min and this failed
# as `exec: executable kubelogin failed with exit code 1`. The azurerm provider
# never hits this — ARM_USE_OIDC fetches its own fresh assertions.
#
# So lifecycle.sh runs scripts/az-refresh.sh before every layer, and the exec
# goes through scripts/kubelogin.sh, which keeps kubelogin's stderr (Terraform
# otherwise drops it; lifecycle.sh prints it on failure) and, in CI, refreshes
# and retries once. Locally both are pass-throughs to your own `az login`.
provider "kubernetes" {
  host                   = try(local.kube.host, "")
  cluster_ca_certificate = try(base64decode(local.kube.cluster_ca_certificate), "")

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "bash"
    args = [
      "${abspath(path.root)}/scripts/kubelogin.sh",
      "get-token",
      "--login", "azurecli",
      # The fixed AKS AAD server application ID. Not a Sentinel value — it is the
      # same for every Azure Kubernetes Service cluster.
      "--server-id", "6dae42f8-4368-4678-94ff-3960e28e3630",
    ]
  }
}

resource "kubernetes_namespace" "deployment" {
  count = local.c_namespace

  metadata {
    name = module.naming.names.kubernetes_namespace

    labels = {
      "sentinel.dev/deployment"  = var.deployment
      "sentinel.dev/environment" = var.environment
      "managed-by"               = "terraform"
    }
  }
}

# Caps what this deployment can take from the shared node.
resource "kubernetes_resource_quota" "deployment" {
  count = local.c_namespace

  metadata {
    name      = "sentinel-quota"
    namespace = kubernetes_namespace.deployment[0].metadata[0].name
  }

  spec {
    hard = {
      "requests.cpu"    = try(local.cfg.namespace.cpu_request, "250m")
      "requests.memory" = try(local.cfg.namespace.memory_request, "512Mi")
      "limits.cpu"      = try(local.cfg.namespace.cpu_limit, "1")
      "limits.memory"   = try(local.cfg.namespace.memory_limit, "2Gi")
      "pods"            = try(local.cfg.namespace.max_pods, "10")
    }
  }
}

# Supplies the per-container defaults the quota above then requires. Without it,
# the first Deployment that omits `resources:` is rejected at admission with a
# message about exceeded quota, which points at the wrong thing entirely.
resource "kubernetes_limit_range" "deployment" {
  count = local.c_namespace

  metadata {
    name      = "sentinel-defaults"
    namespace = kubernetes_namespace.deployment[0].metadata[0].name
  }

  spec {
    limit {
      type = "Container"
      default = {
        cpu    = "200m"
        memory = "256Mi"
      }
      default_request = {
        cpu    = "100m"
        memory = "128Mi"
      }
    }
  }
}

# Default-deny ingress, with same-namespace traffic explicitly allowed.
#
# Namespaces are NOT a network boundary by default in Kubernetes — every pod can
# reach every other pod cluster-wide unless a policy says otherwise. On a shared
# cluster that means one deployment's pod could reach another's, which would
# undo at the network layer the isolation the token model provides.
#
# ENFORCED by the cluster's Cilium data plane (modules/aks, network_profile).
# Before that existed this object was stored and ignored: Kubernetes accepts a
# NetworkPolicy whether or not any engine enforces it, and nothing says so.
#
# Enforcement cuts both ways. Traffic from OUTSIDE the cluster, including a
# public LoadBalancer Service, matches no namespace selector here and is
# dropped too. The backend's LoadBalancer (backend §8.5) therefore needs its
# own allow rule for its pods and port before it can be reached. Not added
# here: that rule belongs with the backend's manifests and labels.
resource "kubernetes_network_policy" "default_deny_ingress" {
  count = local.c_namespace

  metadata {
    name      = "default-deny-ingress"
    namespace = kubernetes_namespace.deployment[0].metadata[0].name
  }

  spec {
    pod_selector {}
    policy_types = ["Ingress"]

    ingress {
      from {
        namespace_selector {
          match_labels = {
            "sentinel.dev/deployment" = var.deployment
          }
        }
      }
    }
  }
}

# The consumer half of the workload-identity pair. The annotation is what binds
# a pod's projected token to the UAMI; without it the pod gets a token Entra
# does not recognise and every Azure call returns 401.
resource "kubernetes_service_account" "backend" {
  count = local.c_namespace

  metadata {
    name      = "sentinel-backend"
    namespace = kubernetes_namespace.deployment[0].metadata[0].name

    annotations = {
      "azure.workload.identity/client-id" = azurerm_user_assigned_identity.backend[0].client_id
    }

    labels = {
      "azure.workload.identity/use" = "true"
    }
  }
}
