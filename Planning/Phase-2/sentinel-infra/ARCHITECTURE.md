# sentinel-infra — Architecture Document

> **↑ Deep dive of the [Architecture Index](../ARCHITECTURE.md).** Start there for the whole
> picture; this file is the authoritative detail for **infra + identity** concerns (index §4 map).

> **Purpose:** Terraform IaC that provisions all Azure resources Sentinel depends on.
> Single `terraform apply` brings up the entire stack. Also manages CI runner
> images in ACR and cross-repo secret distribution.
>
> **Note:** Includes AKS — the sentinel backend runs as a single-replica Deployment
> on a 1-node AKS cluster (free control plane + 1× B2ats_v2 node, free 12 months).
> Terraform provisions the cluster; the sentinel repo's `ci_backend_deployment.yml`
> deploys the app onto it.

---

## 1. System Overview

```
sentinel-infra repo
       │
       │  terraform apply (OIDC — no stored secrets)
       ▼
┌──────────────────────────────────────────────────────────────────┐
│                        Azure (sentinel-rg)                       │
│                                                                   │
│  ┌─────────────────────────┐    ┌──────────────────────────────┐ │
│  │  Azure Container        │    │  Key Vault                    │ │
│  │  Registry (ACR)         │    │  (always free)                │ │
│  │  (free 12 months)       │    │                               │ │
│  │                         │    │  Secrets:                     │ │
│  │  Images:                │    │  ├── anthropic-api-key  ↻     │ │
│  │  ├── sentinel-backend   │    │  ├── openai-api-key     ↻     │ │
│  │  └── ci-runner          │    │  ├── dd-app-key   (no db-pwd) │ │
│  └─────────────────────────┘    │  ├── dd-api-key               │ │
│                                  │  ├── teams-webhook-url        │ │
│  ┌─────────────────────────┐    │  ├── langfuse-secret-key      │ │
│  │  PostgreSQL B1MS        │    │  ├── langfuse-public-key      │ │
│  │  (free 12 months)       │    │  ├── acr-password              │ │
│  │                         │    │  ├── github-pat                │ │
│  │  DB: sentinel (Entra)   │    │  (↻ = rotated; no api-token)  │ │
│  │  Extension: pgvector    │    └──────────────────────────────┘ │
│  │  32 GB storage          │    ┌──────────────────────────────┐ │
│  │  Firewall: allow all    │    │  OIDC Federation              │ │
│  │  (dev — see §3.2)       │    │  (Azure AD App Registration) │ │
│  └─────────────────────────┘    │                               │ │
│                                  │  Trusts:                      │ │
│  ┌─────────────────────────┐    │  ├── sentinel-infra (plan/    │ │
│  │  Event Grid Topic       │    │  │   apply workflows)         │ │
│  │  (always free)          │    │  ├── sentinel (ci/cd/         │ │
│  │  100K ops/month         │    │  │   incident workflows)      │ │
│  └─────────┬───────────────┘    │  └── sentinel-deployment      │ │
│            │                     │      (deploy workflows)       │ │
│            ▼                     └──────────────────────────────┘ │
│  ┌─────────────────────────┐                                      │
│  │  Azure Function         │                                      │
│  │  (always free)          │                                      │
│  │  1M reqs/month          │                                      │
│  │                         │                                      │
│  │  Event Grid →           │                                      │
│  │  repository_dispatch    │                                      │
│  └─────────────────────────┘                                      │
│                                                                   │
│  ┌─────────────────────────┐                                      │
│  │  App Service (F1)       │                                      │
│  │  (always free)          │                                      │
│  │  dummy-api              │    ← sentinel-deployment target      │
│  └─────────────────────────┘                                      │
│                                                                   │
│  ┌─────────────────────────┐                                      │
│  │  AKS (sentinel-aks)     │                                      │
│  │  control plane: free    │    ← sentinel-backend runs here      │
│  │  1× B2ats_v2 node       │      (single replica, public LB IP,  │
│  │  (free 12 months)       │       deployed by sentinel repo CI)  │
│  └─────────────────────────┘                                      │
└──────────────────────────────────────────────────────────────────┘

Backend hosting: AKS (modules/aks/). The sentinel repo's
ci_backend_deployment.yml deploys the sentinel-backend image from ACR
to the cluster on every merge to main. Terraform provisions the cluster
only — app manifests live in the sentinel repo (k8s/).
```

---

## 2. Terraform Module Structure

One module per Azure resource group concern. Flat structure — no nested modules.
7 modules total. Beyond the modules, the root also declares the **identity plane**
(§4): the OIDC SP + federated creds, the backend API app registration (§4.4), the
backend workload-identity UAMI + federated credential (§3.7), and the Key Vault
rotation Function + system topic (§3.8) — the last two live in the `functions/`
module.

```
sentinel-infra/
├── main.tf                    # Provider config, resource group, module calls
├── variables.tf               # Input variables (subscription_id, location, etc.)
├── outputs.tf                 # Outputs (ACR URL, DB host, etc.)
├── terraform.tfvars           # Dev environment values (gitignored)
├── backend.tf                 # Remote state config (Azure Storage)
│
├── modules/
│   ├── acr/                   # Container Registry
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   │
│   ├── postgresql/            # PostgreSQL Flexible Server + DB + pgvector
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   │
│   ├── keyvault/              # Key Vault + secrets + access policies
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   │
│   ├── event-grid/            # Event Grid topic + subscription
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   │
│   ├── functions/             # Azure Function Apps: Event Grid → GHA bridge (§3.5)
│   │   ├── main.tf            #   + Key Vault rotation Function (§3.8)
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   └── src/               # Function source code (Python)
│   │       ├── bridge/        #   Event Grid → repository_dispatch (stamps signal_type)
│   │       │   ├── __init__.py
│   │       │   └── function.json
│   │       └── rotate/        #   SecretNearExpiry → rotate LLM key version
│   │           ├── __init__.py
│   │           └── function.json
│   │
│   ├── app-service/           # App Service F1 for sentinel-deployment
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   │
│   └── aks/                   # AKS cluster — hosts sentinel-backend (1 node)
│       ├── main.tf
│       ├── variables.tf
│       └── outputs.tf
│
├── ci-images/                 # Dockerfiles for CI runner images
│   ├── ci-runner.Dockerfile   # Python 3.12 + ruff + pyright + pytest + az cli
│   └── build-push.sh          # Build and push runner images to ACR
│
├── .github/
│   └── workflows/
│       ├── ci_infra_dry.yml   # terraform validate + plan on push/PR (dry run)
│       ├── ci_infra.yml       # terraform apply on merge to main
│       ├── ci_destroy_infra.yml # manual full teardown — destroy + az cleanup (§7.3)
│       └── ci_runners.yml     # Build + push CI runner images when Dockerfile changes
│
├── .gitignore
└── README.md
```

### Why per-resource modules (not monorepo flat)?

Each module is independently testable and has clear inputs/outputs. The root
`main.tf` wires them together. If we later split a module into its own Terraform
workspace (e.g., for different lifecycle), the module boundary is already there.

---

## 3. Module Details

### 3.1 ACR Module

```hcl
resource "azurerm_container_registry" "sentinel" {
  name                = "sentinelacr"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "Standard"    # Free 100 GB for 12 months
  admin_enabled       = true
}
```

**Outputs:** `acr_login_server`, `acr_admin_username`, `acr_admin_password`

**Images stored in ACR:**

| Image | Purpose | Built By |
|-------|---------|----------|
| `sentinel-backend:sha-X` | Backend API container (deployed to AKS — immutable tag pinned by the Deployment) | ci_backend_deployment.yml (sentinel repo) |
| `sentinel-backend:stable` | Bookmark tag for the last fully validated image (humans/debugging only — nothing pulls it at runtime) | ci_backend_deployment.yml |
| `ci-runner:latest` | CI runner with Python 3.12 + dev tools | build-runners.yml (this repo) |

### 3.2 PostgreSQL Module

```hcl
resource "azurerm_postgresql_flexible_server" "sentinel" {
  name                   = "sentinel-pg"
  resource_group_name    = var.resource_group_name
  location               = var.location
  version                = "16"
  sku_name               = "B_Standard_B1ms"  # Free 750 hrs/mo for 12 months
  storage_mb             = 32768               # 32 GB (free tier limit)
  zone                   = "1"

  # Microsoft Entra–ONLY authentication — no admin password stored anywhere.
  # Every client (GHA runners, the backend pod) presents a short-lived Entra
  # access token (audience https://ossrdbms-aad.database.windows.net) as the
  # psql password. User/SP tokens live ~1 h; managed-identity tokens ~24 h.
  # Password auth is disabled for the server entirely.
  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false
  }
}

# Entra administrator = a security group, so members are added/removed centrally
# in Entra without touching the server. Group holds: Keshav (human break-glass),
# the sentinel-gha SP (GHA runners), and the backend pod's workload-identity
# UAMI (§3.7). Only an Entra admin can create further Entra DB roles.
resource "azurerm_postgresql_flexible_server_active_directory_administrator" "sentinel" {
  server_name         = azurerm_postgresql_flexible_server.sentinel.name
  resource_group_name = var.resource_group_name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  object_id           = var.postgres_entra_admin_group_object_id
  principal_name      = "sentinel-db-admins"
  principal_type      = "Group"

  # Entra auth must be ON before any Entra DB role is created (Terraform ordering).
  depends_on = [azurerm_postgresql_flexible_server.sentinel]
}

resource "azurerm_postgresql_flexible_server_database" "sentinel" {
  name      = "sentinel"
  server_id = azurerm_postgresql_flexible_server.sentinel.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# Enable pgvector extension
resource "azurerm_postgresql_flexible_server_configuration" "pgvector" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.sentinel.id
  value     = "VECTOR"
}

# Firewall: allow all (dev). Network reach ≠ auth — every connection still needs
# a valid Entra token (password auth is disabled), so an open firewall only
# controls who can *reach* the port, not who can *log in*.
# Two client classes connect: (1) the backend on AKS (workload-identity token),
# (2) GHA workflows on GitHub-hosted runners — ci_app_deployment.yml inserts
# deploy rows, ci_incident_response.yml fetches context + records PR refs
# (each acquires an Entra DB token from the sentinel-gha SP).
# GitHub runner IPs rotate across a wide range. During dev we allow all.
# Production would use Private Endpoint + VNet integration.
resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_all_dev" {
  name             = "allow-all-dev"
  server_id        = azurerm_postgresql_flexible_server.sentinel.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "255.255.255.255"
}
```

**Why allow-all?** GHA workflows talk to PostgreSQL directly (deploy recording,
context fetches, PR-reference updates) from GitHub-hosted runners, whose IPs rotate
across a wide CIDR range that changes weekly. There's no stable IP to whitelist.
The `0.0.0.0/0.0.0.0` rule (Azure services only) doesn't cover GitHub runners —
they're not Azure services. The AKS backend's egress IP alone isn't enough. Options:

| Approach | Dev? | Production? |
|----------|------|-------------|
| Allow all (`0.0.0.0`–`255.255.255.255`) | **Yes — simple, works** | No |
| GitHub meta API IP ranges (dynamic) | Fragile — IPs change | No |
| Azure-hosted self-hosted runner | Works but costs money | Maybe |
| Private Endpoint + VNet | Overkill for dev | **Yes** |

We use allow-all for dev. The DB is still protected by **Entra token auth** —
password auth is off, so reaching the port is useless without a valid token
from a member of the `sentinel-db-admins` group (or an Entra role it created).
Lock down the network later with Private Endpoint if needed.

**Entra DB roles (created once by the Entra admin, not by Terraform):** after the
server exists, the admin connects and runs `SELECT * FROM pgaadauth_create_principal(...)`
(or `CREATE ROLE ... ; SECURITY LABEL ...`) to map the `sentinel-gha` SP and the
backend UAMI to PostgreSQL roles with the right grants. Azure matches the token to
the role by the principal's Entra object ID, not its name.

**Connecting (token as password):**
```bash
# GHA runner (after azure/login OIDC) or any Entra principal
PGPASSWORD=$(az account get-access-token \
  --resource https://ossrdbms-aad.database.windows.net \
  --query accessToken -o tsv)
psql "host=sentinel-pg.postgres.database.azure.com dbname=sentinel \
      user=sentinel-gha sslmode=require"   # token supplied via PGPASSWORD
```

**Outputs:** `db_host`, `db_name`, `db_port` (no password output — there is none)

### 3.3 Key Vault Module

```hcl
resource "azurerm_key_vault" "sentinel" {
  name                       = "sentinel-kv"
  location                   = var.location
  resource_group_name        = var.resource_group_name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"  # Always free (10K txns)
  enable_rbac_authorization  = true
}

# Terraform SP — full secret management (set secrets during apply)
resource "azurerm_role_assignment" "terraform_kv_admin" {
  scope                = azurerm_key_vault.sentinel.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

# GHA SP — read-only (ci_incident_response.yml fetches secrets at runtime)
resource "azurerm_role_assignment" "gha_kv_reader" {
  scope                = azurerm_key_vault.sentinel.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azuread_service_principal.sentinel_gha.object_id
}

# Backend pod (workload-identity UAMI, §3.7) — read-only. The pod reads LLM keys
# directly from Key Vault via workload identity, so no secret needs to be baked
# into the K8s Secret for those. No stored credential in the pod.
resource "azurerm_role_assignment" "backend_kv_reader" {
  scope                = azurerm_key_vault.sentinel.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.backend.principal_id
}

# Rotation Function (system-assigned MI, §3.8) — Officer, writes new secret
# versions when a SecretNearExpiry event fires.
resource "azurerm_role_assignment" "rotator_kv_officer" {
  scope                = azurerm_key_vault.sentinel.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = azurerm_linux_function_app.rotator.identity[0].principal_id
}
```

**Access levels:**

| Identity | Role | Purpose |
|----------|------|---------|
| Terraform SP | `Key Vault Secrets Officer` | Create/update secrets + set rotation policies during `terraform apply` |
| GHA SP (OIDC) | `Key Vault Secrets User` | Read-only — `az keyvault secret show` in ci_incident_response.yml |
| Backend UAMI (workload identity) | `Key Vault Secrets User` | Read-only — pod reads LLM keys at runtime, no baked-in secret |
| Rotation Function (system MI) | `Key Vault Secrets Officer` | Writes rotated secret versions (§3.8) |

The GHA SP is the same OIDC-federated identity used by all three repos'
workflows. It can read secrets but never modify them — only Terraform (setup)
and the rotation Function (rotation) write secrets.

**Secrets stored:**

| Secret Name | Description | Consumed By | Rotation |
|-------------|-------------|-------------|----------|
| `anthropic-api-key` | Anthropic LLM provider (default) | Backend (reads via workload identity) | **Rotation policy** (§3.8) |
| `openai-api-key` | OpenAI LLM provider (fallback) | Backend (reads via workload identity) | **Rotation policy** (§3.8) |
| `dd-api-key` | Datadog API key | GHA jobs (Datadog event reporting) | Manual |
| `dd-app-key` | Datadog app key | GHA jobs (Datadog log queries) | Manual |
| `teams-webhook-url` | Teams incoming webhook | GHA jobs (notifications) | Manual |
| `langfuse-secret-key` | LangFuse tracing (secret) | Backend (workload identity) | Manual |
| `langfuse-public-key` | LangFuse tracing (public) | Backend (workload identity) | Manual |
| `acr-password` | ACR admin password | GHA jobs (`container:` image pull) | Manual |
| `github-pat` | GitHub PAT with `repo` scope | Azure Function bridge (repository_dispatch) | Manual |

**Removed vs the previous design:**
- ❌ `db-password` — **gone.** PostgreSQL is Entra-only (§3.2); clients present a
  short-lived Entra token as the password. Nothing stores a DB password.
- ❌ `sentinel-api-token` — **gone.** The backend API is protected by Entra bearer
  tokens (§4.5), validated against Entra JWKS. No shared static token exists.

**Still standing (honest accounting):** `acr-password` and `github-pat` remain
genuine secrets. `acr-password` is needed because GHA `container:` jobs pull the
CI-runner image *before* any step runs (can't `az acr login` first); `github-pat`
is needed by the Function bridge's `repository_dispatch` and Terraform's github
provider. Both are bootstrap-class, not runtime-request credentials. Eliminating
them fully would need a GitHub App + Azure-hosted runners (out of scope for now).

**Secret flow 1: backend pod → Key Vault + PostgreSQL via workload identity (runtime):**

```
ci_backend_deployment.yml (sentinel repo, merge to main) — deploy-to-aks job:
│
├── kubectl set image deployment/sentinel-backend sentinel-backend=<ACR>:sha-X
├── kubectl rollout status deployment/sentinel-backend
│
└── validate: curl $BACKEND_URL/health + /ready  (checks DB token + LangFuse)

NO secret sync — no K8s Secret is created. The pod runs under a workload-identity
service account (§3.7). At runtime it exchanges its projected SA token for an
Entra token and:
  • reads anthropic-api-key / openai-api-key / langfuse-* straight from Key Vault
  • gets a PostgreSQL token (aud https://ossrdbms-aad.database.windows.net) for DB
Nothing sensitive is baked into the Deployment manifest or a K8s Secret.
```

**Secret flow 2: GHA jobs acquire per-job tokens/secrets (incident runtime):**

```
ci_incident_response.yml — each job acquires ONLY what it needs, in-job.
(GHA drops masked secrets from job outputs — never pass secrets between jobs.)
│
├── fetch-service-info:   az account get-access-token --resource \
│                           https://ossrdbms-aad.database.windows.net   (Entra DB token → psql read)
├── fetch-datadog-logs:   az keyvault secret show --name dd-api-key
├── run-agent-pipeline:   az account get-access-token --resource \
│                           api://sentinel-backend                      (Entra token → Authorization: Bearer)
├── create-rollback-pr:   Entra DB token (psql UPDATE) + github-pat (open PR)
└── notify-* / summary:   az keyvault secret show --name teams-webhook-url

ci_app_deployment.yml (sentinel-deployment repo):
└── record-deployment:    az account get-access-token --resource \
                            https://ossrdbms-aad.database.windows.net   (Entra DB token → psql INSERT)
```

Two token audiences, one identity: the `sentinel-gha` SP asks Entra for a
DB-scoped token *and* an `api://sentinel-backend`-scoped token as needed — see
§4.5 "one identity, many audience-scoped tokens."

### 3.4 Event Grid Module

The topic ingests **two distinct Datadog signal types**, both from the
sentinel-deployment ground-truth app (see sentinel-deployment ARCHITECTURE §4):

| Signal type | Datadog source | Meaning | Backend handling path |
|-------------|----------------|---------|-----------------------|
| `deploy_failure` | `sentinel-deploy-failure` event monitor (`deploy_status:failed`) | The deploy itself failed; the previous good version stays live | **Case 2 — rollback** (simple; heal main's deployability) |
| `runtime_error` | `sentinel-runtime-health` monitor (5xx rate / failed health pings) | Deploy went green but the live app throws errors | **Case 3 — full incident response** (diagnose → rollback/escalate) |

(Case 1 = a clean deploy fires **no** monitor — nothing reaches Event Grid.)

Both monitors POST to the same topic; the bridge Function (§3.5) inspects the
event, stamps `signal_type` onto the `client_payload`, and dispatches to the
sentinel repo. The backend incident workflow branches on `signal_type`.

```hcl
resource "azurerm_eventgrid_topic" "sentinel" {
  name                = "sentinel-events"
  location            = var.location
  resource_group_name = var.resource_group_name
}

# One subscription → the bridge Function. The Function classifies the payload
# into deploy_failure vs runtime_error (it does not need two subscriptions —
# both Datadog monitors carry tags the Function reads).
resource "azurerm_eventgrid_event_subscription" "to_function" {
  name  = "sentinel-to-function"
  scope = azurerm_eventgrid_topic.sentinel.id

  azure_function_endpoint {
    function_id = "${var.function_app_id}/functions/bridge"
  }
}
```

### 3.5 Azure Function Module (Event Grid → GHA Bridge)

```hcl
resource "azurerm_service_plan" "functions" {
  name                = "sentinel-func-plan"
  location            = var.location
  resource_group_name = var.resource_group_name
  os_type             = "Linux"
  sku_name            = "Y1"  # Consumption plan — always free (1M reqs)
}

resource "azurerm_linux_function_app" "bridge" {
  name                = "sentinel-bridge"
  location            = var.location
  resource_group_name = var.resource_group_name
  service_plan_id     = azurerm_service_plan.functions.id

  storage_account_name       = azurerm_storage_account.func.name
  storage_account_access_key = azurerm_storage_account.func.primary_access_key

  site_config {
    application_stack {
      python_version = "3.12"
    }
  }

  app_settings = {
    "GITHUB_TOKEN"      = "@Microsoft.KeyVault(VaultName=sentinel-kv;SecretName=github-pat)"
    "GITHUB_REPO"       = "Keshav0375/Sentinel"
    "GITHUB_EVENT_TYPE" = "incident-alert"
  }
}
```

**Bridge function source (Python):**

```python
import httpx
import azure.functions as func


def _classify(data: dict) -> str:
    """deploy_failure vs runtime_error, from Datadog monitor tags."""
    tags = " ".join(data.get("tags", [])) + data.get("title", "")
    return "deploy_failure" if "deploy_status:failed" in tags else "runtime_error"


def main(event: func.EventGridEvent):
    """Event Grid → GitHub repository_dispatch, stamped with signal_type."""
    data = event.get_json()
    signal_type = _classify(data)

    httpx.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/dispatches",
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        },
        json={
            "event_type": GITHUB_EVENT_TYPE,
            "client_payload": {**data, "signal_type": signal_type},
        },
    )
```

`signal_type` drives the backend workflow branch: `deploy_failure` → Case 2
(rollback), `runtime_error` → Case 3 (full incident response).

### 3.6 App Service Module (for sentinel-deployment)

```hcl
resource "azurerm_service_plan" "deployment" {
  name                = "sentinel-deploy-plan"
  location            = var.location
  resource_group_name = var.resource_group_name
  os_type             = "Linux"
  sku_name            = "F1"  # Always free
}

resource "azurerm_linux_web_app" "dummy_api" {
  name                = "dummy-api"
  location            = var.location
  resource_group_name = var.resource_group_name
  service_plan_id     = azurerm_service_plan.deployment.id

  site_config {
    application_stack {
      python_version = "3.12"
    }
  }

  app_settings = {
    "APP_VERSION" = "initial"
    "DD_SERVICE"  = "dummy-api"
    "DD_ENV"      = "dev"
    "SCM_DO_BUILD_DURING_DEPLOYMENT" = "true"
  }
}
```

### 3.7 AKS Module (Backend Hosting)

```hcl
resource "azurerm_kubernetes_cluster" "sentinel" {
  name                = "sentinel-aks"
  location            = var.location
  resource_group_name = var.resource_group_name
  dns_prefix          = "sentinel"
  sku_tier            = "Free"          # control plane — always free

  # Workload identity: the cluster becomes an OIDC issuer; pods federate their
  # projected service-account token to an Entra identity for passwordless access
  # to Key Vault + PostgreSQL. No stored secret in the pod.
  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  default_node_pool {
    name       = "default"
    node_count = 1
    vm_size    = "Standard_B2ats_v2"    # free 750 hrs/mo for 12 months
  }

  identity {
    type = "SystemAssigned"
  }

  # Scale-to-zero: sentinel repo workflows scale the node pool 0↔1 at runtime
  # (backend-up/backend-down actions + nightly auto-down). Don't let
  # terraform apply fight them over node_count.
  lifecycle {
    ignore_changes = [default_node_pool[0].node_count]
  }
}

# Kubelet pulls images from ACR without imagePullSecrets
resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                = azurerm_container_registry.sentinel.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.sentinel.kubelet_identity[0].object_id
}

# GHA OIDC SP can deploy (az aks get-credentials + kubectl)
resource "azurerm_role_assignment" "gha_aks_user" {
  scope                = azurerm_kubernetes_cluster.sentinel.id
  role_definition_name = "Azure Kubernetes Service Cluster User Role"
  principal_id         = azuread_service_principal.sentinel_gha.object_id
}

# ── Backend workload identity ────────────────────────────────────────────────
# User-assigned identity the backend pod runs as. It gets Key Vault Secrets User
# (§3.3) and is a member of sentinel-db-admins for PostgreSQL (§3.2).
resource "azurerm_user_assigned_identity" "backend" {
  name                = "sentinel-backend-wi"
  location            = var.location
  resource_group_name = var.resource_group_name
}

# Federate the K8s service account to the UAMI. Subject is the SA that the
# Deployment runs under; audience is the workload-identity exchange audience.
resource "azurerm_federated_identity_credential" "backend" {
  name                = "sentinel-backend-fic"
  resource_group_name = var.resource_group_name
  parent_id           = azurerm_user_assigned_identity.backend.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.sentinel.oidc_issuer_url
  subject             = "system:serviceaccount:sentinel:sentinel-backend"  # namespace:sa
}
```

The K8s Deployment references this via a ServiceAccount annotated with
`azure.workload.identity/client-id = <backend UAMI client id>` and the pod label
`azure.workload.identity/use: "true"` (manifests live in the sentinel repo,
`azure/k8s/`). Outputs `backend_identity_client_id` for the sentinel repo to
stamp into the ServiceAccount annotation.

**Lifecycle split:** Terraform provisions the cluster only. The `sentinel-backend`
Deployment/Service manifests live in the sentinel repo (`azure/k8s/`) and are applied by
`ci_backend_deployment.yml` — app deployment is CI's job, not Terraform's. Infra
changes rarely; the app deploys on every merge.

**Outputs:** `aks_cluster_name`, `aks_resource_group`, `oidc_issuer_url`,
`backend_identity_client_id`

---

### 3.8 Rotation Function Module (LLM key rotation)

Azure Key Vault has **no dynamic secret engine** (that is a HashiCorp Vault
concept). It stores static secret *versions* with a **rotation policy** plus an
Event Grid `SecretNearExpiry` notification. We use that to rotate the LLM keys
on a schedule — rotation, not on-demand generation (no LLM provider mints
ephemeral keys).

```hcl
# Rotation policy: fire SecretNearExpiry 30 days before expiry.
resource "azurerm_key_vault_secret" "anthropic" {
  name         = "anthropic-api-key"
  key_vault_id = azurerm_key_vault.sentinel.id
  value        = var.anthropic_api_key
  expiration_date = timeadd(timestamp(), "2160h")  # ~90 days; seeded, then rotator manages
}

# A second Function App (Consumption, always free) with a system-assigned MI.
resource "azurerm_linux_function_app" "rotator" {
  name                = "sentinel-rotator"
  location            = var.location
  resource_group_name = var.resource_group_name
  service_plan_id     = azurerm_service_plan.functions.id   # shares the bridge's Y1 plan

  storage_account_name       = azurerm_storage_account.func.name
  storage_account_access_key = azurerm_storage_account.func.primary_access_key

  identity { type = "SystemAssigned" }        # → Key Vault Secrets Officer (§3.3)

  site_config { application_stack { python_version = "3.12" } }
}

# Event Grid: Key Vault SecretNearExpiry → rotator Function.
resource "azurerm_eventgrid_system_topic" "kv" {
  name                   = "sentinel-kv-events"
  location               = var.location
  resource_group_name    = var.resource_group_name
  source_arm_resource_id = azurerm_key_vault.sentinel.id
  topic_type             = "Microsoft.KeyVault.vaults"
}

resource "azurerm_eventgrid_system_topic_event_subscription" "rotate" {
  name                = "sentinel-rotate-on-near-expiry"
  system_topic        = azurerm_eventgrid_system_topic.kv.name
  resource_group_name = var.resource_group_name
  included_event_types = ["Microsoft.KeyVault.SecretNearExpiry"]

  azure_function_endpoint {
    function_id = "${azurerm_linux_function_app.rotator.id}/functions/rotate"
  }
}
```

**Rotator Function logic (Python):** on `SecretNearExpiry`, read the secret name
from the event → call the provider's key-management API to mint a fresh key
(Anthropic Admin API `/v1/organizations/api_keys` where org-admin is available;
otherwise the function logs a "manual rotation required" alert to Teams) →
`set_secret` a new version with a fresh 90-day expiry. The backend always reads
the *latest* version via workload identity, so it picks up rotated keys with no
redeploy.

> **Honesty note:** true zero-touch rotation needs a provider key API. Anthropic
> exposes an Admin API for API-key management on qualifying plans; if that is not
> available on the student/dev account, the rotator degrades to a scheduled
> reminder rather than silently doing nothing.

**Outputs:** none consumed downstream (self-contained).

---

## 4. Identity Plane — OIDC + Entra (Workload Identity Federation)

All three repos authenticate to Azure via OIDC — no stored client secrets.
GitHub proves identity via JWT, Azure trusts it via federated credentials.

### 4.1 How OIDC works

```
GHA workflow                Azure AD
    │                           │
    ├── request JWT token ──►   │
    │   (from GitHub OIDC       │
    │    provider)              │
    │                           │
    ├── az login with JWT ──►   │  verifies issuer = GitHub
    │                           │  verifies subject = repo:org/name:ref:refs/heads/main
    │                           │
    │   ◄── access token ──────┤  scoped to sentinel-rg
    │                           │
    ├── use token for:          │
    │   ├── terraform apply     │
    │   ├── az keyvault secret  │
    │   ├── az acr login        │
    │   └── docker push/pull    │
```

### 4.2 Terraform resources for OIDC

> **Casing matters.** The GitHub OIDC token's `sub` claim uses the repository's canonical
> owner/name (`Keshav0375/Sentinel-infra`, `Keshav0375/Sentinel-deployment`,
> `Keshav0375/Sentinel`), and Azure matches federated-credential subjects as an exact,
> case-sensitive string. Use the real casing below verbatim.

```hcl
# Azure AD Application
resource "azuread_application" "sentinel_gha" {
  display_name = "sentinel-gha-oidc"
}

resource "azuread_service_principal" "sentinel_gha" {
  client_id = azuread_application.sentinel_gha.client_id
}

# Contributor on resource group
resource "azurerm_role_assignment" "gha_contributor" {
  scope                = azurerm_resource_group.sentinel.id
  role_definition_name = "Contributor"
  principal_id         = azuread_service_principal.sentinel_gha.object_id
}

# Federated credentials — one per repo + trigger combination
resource "azuread_application_federated_identity_credential" "sentinel_infra_main" {
  application_id = azuread_application.sentinel_gha.id
  display_name   = "sentinel-infra-main"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:Keshav0375/Sentinel-infra:ref:refs/heads/main"
}

resource "azuread_application_federated_identity_credential" "sentinel_infra_pr" {
  application_id = azuread_application.sentinel_gha.id
  display_name   = "sentinel-infra-pr"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:Keshav0375/Sentinel-infra:pull_request"
}

resource "azuread_application_federated_identity_credential" "sentinel_main" {
  application_id = azuread_application.sentinel_gha.id
  display_name   = "sentinel-main"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:Keshav0375/Sentinel:ref:refs/heads/main"
}

resource "azuread_application_federated_identity_credential" "sentinel_pr" {
  application_id = azuread_application.sentinel_gha.id
  display_name   = "sentinel-pr"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:Keshav0375/Sentinel:pull_request"
}

resource "azuread_application_federated_identity_credential" "sentinel_deployment_main" {
  application_id = azuread_application.sentinel_gha.id
  display_name   = "sentinel-deployment-main"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:Keshav0375/Sentinel-deployment:ref:refs/heads/main"
}
```

### 4.3 Chicken-and-egg: bootstrapping OIDC

The OIDC federated credential itself is a Terraform resource — but Terraform
needs Azure access to create it. Bootstrap sequence:

1. **Manual (one-time):** Create SP + first federated credential via `az` CLI
2. **Import into Terraform:** `terraform import azuread_application.sentinel_gha <app-id>`
3. **After import:** Terraform manages all subsequent federated credentials

```bash
# One-time bootstrap (run manually)
az ad app create --display-name sentinel-gha-oidc
APP_ID=$(az ad app list --display-name sentinel-gha-oidc --query '[0].appId' -o tsv)

az ad sp create --id $APP_ID
SP_OBJ_ID=$(az ad sp show --id $APP_ID --query 'id' -o tsv)

# Assign Contributor on resource group
az role assignment create --assignee $SP_OBJ_ID \
  --role Contributor \
  --scope /subscriptions/<sub-id>/resourceGroups/sentinel-rg

# Create first federated credential for sentinel-infra main branch
az ad app federated-credential create --id $APP_ID --parameters '{
  "name": "sentinel-infra-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:Keshav0375/Sentinel-infra:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'

# Also for PR (so ci_infra.yml can run terraform plan)
az ad app federated-credential create --id $APP_ID --parameters '{
  "name": "sentinel-infra-pr",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:Keshav0375/Sentinel-infra:pull_request",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

After this, add `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
to sentinel-infra GitHub repo secrets. Then `ci_infra.yml` can run and Terraform
manages all remaining federated credentials for the other repos.

### 4.4 Backend API app registration (inbound Entra bearer auth)

The sentinel backend is registered as its own Entra app so callers can obtain
tokens **scoped to it** and the backend can validate them against Entra JWKS —
no shared static token (this is what deleted `sentinel-api-token`).

```hcl
# The backend API as an Entra app: audience = api://sentinel-backend, with an
# app role that callers must hold.
resource "azuread_application" "sentinel_backend" {
  display_name    = "sentinel-backend-api"
  identifier_uris = ["api://sentinel-backend"]

  app_role {
    allowed_member_types = ["Application"]
    display_name         = "Incident.Write"
    description          = "Call the incident pipeline and write results"
    value                = "Incident.Write"
    id                   = "11111111-1111-1111-1111-111111111111"  # stable GUID
    enabled              = true
  }
}

resource "azuread_service_principal" "sentinel_backend" {
  client_id = azuread_application.sentinel_backend.client_id
}

# Grant the GHA identity the Incident.Write app role on the backend API.
resource "azuread_app_role_assignment" "gha_incident_write" {
  app_role_id         = "11111111-1111-1111-1111-111111111111"
  principal_object_id = azuread_service_principal.sentinel_gha.object_id
  resource_object_id  = azuread_service_principal.sentinel_backend.object_id
}
```

**What the backend validates** (FastAPI dependency, sentinel repo §—): signature
against `https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys`, `iss` =
your tenant, `aud` = `api://sentinel-backend`, `exp` not passed, and the
`roles` claim contains `Incident.Write`. `/health` and `/ready` stay open (K8s
probes can't carry tokens). Tenant ID + audience are **public config, not
secrets** — pushed to the sentinel repo as GitHub *variables*, not secrets.

### 4.5 One identity, many audience-scoped tokens

The `sentinel-gha` SP is a **single identity** that requests a **different token
per destination**. A token stamped for one audience is rejected by another —
this is by design and limits blast radius. There is no single reusable token.

| GHA is talking to… | `--resource` / audience | How obtained |
|--------------------|-------------------------|--------------|
| Azure control plane (terraform, `az keyvault`, `az aks`) | `https://management.azure.com` | `azure/login` default |
| Sentinel backend API | `api://sentinel-backend` | `az account get-access-token --resource api://sentinel-backend` |
| PostgreSQL | `https://ossrdbms-aad.database.windows.net` | `az account get-access-token --resource <that>` |

The backend pod does the same via **workload identity** (§3.7): one UAMI, two
audience-scoped tokens (Key Vault, PostgreSQL).

---

## 5. Cross-Repo Secret Distribution

After `terraform apply`, the sentinel and sentinel-deployment repos need ACR
credentials and other values as GitHub Actions secrets. Terraform pushes these
automatically using the GitHub provider — no manual copy-paste.

### 5.1 GitHub provider config

```hcl
provider "github" {
  token = var.github_pat
  owner = "Keshav0375"
}
```

### 5.2 Secrets pushed to sentinel repo

```hcl
resource "github_actions_secret" "sentinel_acr_login_server" {
  repository      = "Sentinel"
  secret_name     = "ACR_LOGIN_SERVER"
  plaintext_value = azurerm_container_registry.sentinel.login_server
}

resource "github_actions_secret" "sentinel_acr_username" {
  repository      = "Sentinel"
  secret_name     = "ACR_USERNAME"
  plaintext_value = azurerm_container_registry.sentinel.admin_username
}

resource "github_actions_secret" "sentinel_acr_password" {
  repository      = "Sentinel"
  secret_name     = "ACR_PASSWORD"
  plaintext_value = azurerm_container_registry.sentinel.admin_password
}

resource "github_actions_secret" "sentinel_azure_client_id" {
  repository      = "Sentinel"
  secret_name     = "AZURE_CLIENT_ID"
  plaintext_value = azuread_application.sentinel_gha.client_id
}

resource "github_actions_secret" "sentinel_azure_tenant_id" {
  repository      = "Sentinel"
  secret_name     = "AZURE_TENANT_ID"
  plaintext_value = data.azurerm_client_config.current.tenant_id
}

resource "github_actions_secret" "sentinel_azure_subscription_id" {
  repository      = "Sentinel"
  secret_name     = "AZURE_SUBSCRIPTION_ID"
  plaintext_value = data.azurerm_client_config.current.subscription_id
}
```

### 5.3 Secrets pushed to sentinel-deployment repo

```hcl
resource "github_actions_secret" "deployment_azure_client_id" {
  repository      = "Sentinel-deployment"
  secret_name     = "AZURE_CLIENT_ID"
  plaintext_value = azuread_application.sentinel_gha.client_id
}

resource "github_actions_secret" "deployment_azure_tenant_id" {
  repository      = "Sentinel-deployment"
  secret_name     = "AZURE_TENANT_ID"
  plaintext_value = data.azurerm_client_config.current.tenant_id
}

resource "github_actions_secret" "deployment_azure_subscription_id" {
  repository      = "Sentinel-deployment"
  secret_name     = "AZURE_SUBSCRIPTION_ID"
  plaintext_value = data.azurerm_client_config.current.subscription_id
}
```

### 5.4 What flows where

```
terraform apply
    │
    ├── Creates Azure resources (ACR, PostgreSQL, Key Vault, etc.)
    │
    ├── Pushes to sentinel repo GitHub secrets:
    │   ├── ACR_LOGIN_SERVER, ACR_USERNAME, ACR_PASSWORD
    │   ├── AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID
    │   └── (sentinel's GHA uses OIDC + these to access Key Vault at runtime)
    │
    ├── Pushes to sentinel-deployment repo GitHub secrets:
    │   ├── AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID
    │   └── (deployment's GHA uses OIDC + these for az webapp deploy)
    │
    └── Key Vault stores runtime secrets (API keys, DB password, etc.)
        └── fetched at runtime by ci_incident_response.yml via az keyvault secret show
```

**Three layers now:**
- **GitHub repo *variables*** (non-secret) = identity pointers — `AZURE_CLIENT_ID`,
  `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, and `SENTINEL_API_AUDIENCE`
  (`api://sentinel-backend`). These are just IDs; safe to expose.
- **GitHub repo secrets** = the few genuine bootstrap credentials — `ACR_*`,
  `GITHUB_PAT`. No `AZURE_CLIENT_SECRET` (OIDC), no `DB_PASSWORD` (Entra DB auth),
  no `sentinel-api-token` (Entra bearer).
- **Key Vault secrets** = runtime values — LLM API keys (rotated), Datadog keys,
  Teams webhook, LangFuse keys. **No DB password** (Entra token auth).

Terraform also pushes `SENTINEL_API_AUDIENCE` + `AZURE_TENANT_ID` as variables to
the sentinel repo so `ci_incident_response.yml` can request the backend token and
the backend can validate `aud`/`iss`. GitHub identity pointers are set once and
rarely change; Key Vault secrets update independently via `az keyvault secret set`
(or the rotation Function) without re-running Terraform.

---

## 6. CI Runner Images

The sentinel repo's CI workflows need specific tools. Instead of installing them
every run (slow), we build a custom runner image and store it in ACR.

### 6.1 ci-runner.Dockerfile

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gcc libpq-dev git docker.io && rm -rf /var/lib/apt/lists/*

# Dev tools
RUN pip install --no-cache-dir \
    ruff pyright pytest pytest-asyncio \
    asyncpg pgvector alembic

# Azure CLI (for Key Vault access + ACR login)
RUN curl -sL https://aka.ms/InstallAzureCLIDeb | bash
```

### 6.2 Bootstrap: first build is manual

The `build-runners.yml` workflow needs ACR credentials — but those come from
Terraform, which needs ACR to exist first. Sequence:

1. `terraform apply` creates ACR
2. **Manually** build and push the first `ci-runner` image:
   ```bash
   az acr login --name sentinelacr
   docker build -f ci-images/ci-runner.Dockerfile -t sentinelacr.azurecr.io/ci-runner:latest .
   docker push sentinelacr.azurecr.io/ci-runner:latest
   ```
3. After that, `build-runners.yml` handles all subsequent updates automatically

### 6.3 `ci_runners.yml` — Build runner images on change

**Name:** `[infra] runners — build and push`

Triggers when `ci-images/` changes on main:

```yaml
name: "[infra] runners — build and push"

on:
  push:
    branches: [main]
    paths: ['ci-images/**']

permissions:
  id-token: write
  contents: read

jobs:
  build-and-push:
    name: Build and Push Runner Image
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}

      - run: az acr login --name sentinelacr

      - run: |
          docker build -f ci-images/ci-runner.Dockerfile \
            -t sentinelacr.azurecr.io/ci-runner:latest .
          docker push sentinelacr.azurecr.io/ci-runner:latest
```

### 6.4 Usage in sentinel repo workflows

```yaml
# In sentinel repo's ci_validation.yml / ci_backend_deployment.yml:
jobs:
  quality:
    runs-on: ubuntu-latest
    container:
      image: ${{ secrets.ACR_LOGIN_SERVER }}/ci-runner:latest
      credentials:
        username: ${{ secrets.ACR_USERNAME }}
        password: ${{ secrets.ACR_PASSWORD }}
```

---

## 7. CI/CD Workflows

### Naming Convention

Follows the cross-repo standard (defined in sentinel/ARCHITECTURE.md §9):

| File | Name | Purpose |
|------|------|---------|
| `ci_infra_dry.yml` | `[infra] terraform — validate and plan` | Dry run on push/PR |
| `ci_infra.yml` | `[infra] terraform — apply` | Apply on merge to main |
| `ci_destroy_infra.yml` | `[infra] terraform — destroy (full teardown)` | Manual full teardown (§7.3) |
| `ci_runners.yml` | `[infra] runners — build and push` | Build + push CI runner images |

Job IDs: `kebab-case` verb-noun. Job names: Title case.

### 7.1 `ci_infra_dry.yml` — Dry run on push/PR

**Name:** `[infra] terraform — validate and plan`

Validates Terraform config and runs `plan` — never applies. Runs on every push
and PR to catch syntax errors, missing variables, and drift early.

```yaml
name: "[infra] terraform — validate and plan (dry run)"

on:
  push:
    branches: ['**']
  pull_request:
    branches: [main]

permissions:
  id-token: write
  contents: read
  pull-requests: write

jobs:
  run-validate:
    name: Run Validate
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3

      - uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}

      - name: Terraform Init
        run: terraform init

      - name: Terraform Validate
        run: terraform validate

      - name: Terraform Format Check
        run: terraform fmt -check -recursive

  run-plan:
    name: Run Plan
    needs: run-validate
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3

      - uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}

      - name: Terraform Init
        run: terraform init

      - name: Terraform Plan
        id: plan
        run: terraform plan -var="github_pat=${{ secrets.GITHUB_PAT }}" -var="postgres_entra_admin_group_object_id=${{ vars.PG_ADMIN_GROUP_OBJECT_ID }}" -no-color -out=tfplan

      - name: Post Plan to PR
        if: github.event_name == 'pull_request'
        uses: actions/github-script@v7
        with:
          script: |
            const plan = `${{ steps.plan.outputs.stdout }}`;
            const truncated = plan.length > 60000 ? plan.substring(0, 60000) + '\n... (truncated)' : plan;
            github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: `## Terraform Plan\n\`\`\`\n${truncated}\n\`\`\``
            });
```

### 7.2 `ci_infra.yml` — Apply on merge to main

**Name:** `[infra] terraform — apply`

Only runs on merge to main. Applies the plan. Uses GitHub environment
protection rules for an extra approval gate if desired.

```yaml
name: "[infra] terraform — apply"

on:
  push:
    branches: [main]

permissions:
  id-token: write
  contents: read

jobs:
  run-apply:
    name: Run Apply
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3

      - uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}

      - name: Terraform Init
        run: terraform init

      - name: Terraform Apply
        run: terraform apply -auto-approve -var="github_pat=${{ secrets.GITHUB_PAT }}" -var="postgres_entra_admin_group_object_id=${{ vars.PG_ADMIN_GROUP_OBJECT_ID }}"
```

### 7.3 `ci_destroy_infra.yml` — Full teardown (manual)

**Name:** `[infra] terraform — destroy (full teardown)`

The mirror of `ci_infra.yml`. **Decision: "everything, always"** — one run frees
*all* Sentinel resources from Azure, including the OIDC app registration and the
Terraform state. Use it to end the project, or to reset to a clean slate before
re-bootstrapping. It is **manual-only** (`workflow_dispatch`) with a typed
confirmation + environment protection so it can never fire on a push.

**Two-stage destroy** (the state storage is a *manually*-bootstrapped resource
that lives outside the Terraform state it holds, so `terraform destroy` alone
can't remove it):

```yaml
name: "[infra] terraform — destroy (full teardown)"

on:
  workflow_dispatch:
    inputs:
      confirm:
        description: 'Type DESTROY to confirm full teardown'
        required: true

permissions:
  id-token: write
  contents: read

jobs:
  destroy:
    name: Full Teardown
    runs-on: ubuntu-latest
    environment: destroy          # protection rule → manual approval
    if: ${{ inputs.confirm == 'DESTROY' }}
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3

      # Auth is acquired HERE, at job start. The ARM token stays valid ~1 h, so
      # the job completes even after stage 1 deletes the very OIDC app it logged
      # in with.
      - uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}

      # ── Stage 1: destroy everything Terraform manages (incl. imported OIDC app,
      #    federated creds, all 8 modules) ──────────────────────────────────────
      - name: Terraform Init
        run: terraform init
      - name: Terraform Destroy
        run: terraform destroy -auto-approve -var="github_pat=${{ secrets.GITHUB_PAT }}" -var="postgres_entra_admin_group_object_id=${{ vars.PG_ADMIN_GROUP_OBJECT_ID }}"

      # ── Stage 2: az cleanup of the manual bootstrap resources Terraform can't
      #    reach — the resource group leftovers and the state storage RG ─────────
      - name: Delete bootstrap resource groups
        run: |
          az group delete --name sentinel-rg        --yes --no-wait || true
          az group delete --name sentinel-state-rg  --yes --no-wait || true
```

> **Restart cost (accepted with "everything, always"):** because the OIDC app +
> federated creds + state are gone, bringing Sentinel back requires re-running the
> manual bootstrap (§4.3 + §10 steps 1–4) before `ci_infra.yml` can authenticate
> again. This is deliberate — the trade for the simplest teardown mental model.

---

## 8. Terraform State

### 8.1 Remote State (Azure Storage)

```hcl
terraform {
  backend "azurerm" {
    resource_group_name  = "sentinel-state-rg"
    storage_account_name = "sentineltfstate"
    container_name       = "tfstate"
    key                  = "sentinel.terraform.tfstate"
  }
}
```

The state storage account is created manually (one-time bootstrap):
```bash
az group create --name sentinel-state-rg --location eastus
az storage account create --name sentineltfstate --resource-group sentinel-state-rg \
  --sku Standard_LRS --encryption-services blob
az storage container create --name tfstate --account-name sentineltfstate
```

### 8.2 State Locking

Azure Storage provides native state locking via blob leases. No DynamoDB needed.

---

## 9. Required GitHub Secrets (sentinel-infra repo)

**GitHub *variables* (non-secret — just identifiers):**

| Variable | Description | How Set |
|----------|-------------|---------|
| `AZURE_CLIENT_ID` | OIDC app client ID | Manual (from bootstrap §4.3) |
| `AZURE_TENANT_ID` | Azure AD tenant ID | Manual |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID | Manual |
| `PG_ADMIN_GROUP_OBJECT_ID` | Object ID of the `sentinel-db-admins` Entra group | Manual (from bootstrap) |

**GitHub *secrets* (genuine credentials):**

| Secret | Description | How Set |
|--------|-------------|---------|
| `GITHUB_PAT` | GitHub PAT with `repo` scope | Manual (for github_actions_secret provider + Function bridge) |

**No `AZURE_CLIENT_SECRET`** — OIDC eliminates it.
**No `DB_PASSWORD`** — PostgreSQL is Entra-only (§3.2); clients present a token.
**No `sentinel-api-token`** — the backend API uses Entra bearer tokens (§4.4).

---

## 10. Prerequisites & Setup Checklist

### One-time Bootstrap (manual — run once, in order)

1. [ ] Create Azure resource group:
   ```bash
   az group create --name sentinel-rg --location eastus
   ```

2. [ ] Create state storage (§8.1):
   ```bash
   az storage account create --name sentineltfstate --resource-group sentinel-state-rg \
     --sku Standard_LRS --encryption-services blob
   az storage container create --name tfstate --account-name sentineltfstate
   ```

3. [ ] Create OIDC service principal + first federated credential (§4.3)

4. [ ] Create the `sentinel-db-admins` Entra group and note its object ID (this
   becomes the PostgreSQL Entra admin, §3.2). Add yourself now; the GHA SP + the
   backend UAMI are added after step 5 creates them:
   ```bash
   az ad group create --display-name sentinel-db-admins --mail-nickname sentinel-db-admins
   PG_ADMIN_GROUP_OBJECT_ID=$(az ad group show --group sentinel-db-admins --query id -o tsv)
   az ad group member add --group sentinel-db-admins --member-id $(az ad signed-in-user show --query id -o tsv)
   ```

5. [ ] Add GitHub *variables* + `GITHUB_PAT` secret to sentinel-infra repo (§9) —
   incl. `PG_ADMIN_GROUP_OBJECT_ID`. **No** `DB_PASSWORD`.

6. [ ] First `terraform apply` (can be local or via GHA):
   ```bash
   terraform init
   terraform apply -var="github_pat=<your-pat>" \
     -var="postgres_entra_admin_group_object_id=$PG_ADMIN_GROUP_OBJECT_ID"
   ```
   Then add the created SP + backend UAMI to the admin group:
   ```bash
   az ad group member add --group sentinel-db-admins --member-id <sentinel_gha SP object id>
   az ad group member add --group sentinel-db-admins --member-id <backend UAMI principal id>
   ```

7. [ ] Populate Key Vault with runtime secrets (no `db-password`, no
   `sentinel-api-token` — both eliminated):
   ```bash
   az keyvault secret set --vault-name sentinel-kv --name anthropic-api-key --value "sk-ant-..." --expires "$(date -u -d '+90 days' +%Y-%m-%dT%H:%M:%SZ)"
   az keyvault secret set --vault-name sentinel-kv --name openai-api-key --value "sk-..." --expires "$(date -u -d '+90 days' +%Y-%m-%dT%H:%M:%SZ)"
   az keyvault secret set --vault-name sentinel-kv --name dd-api-key --value "..."
   az keyvault secret set --vault-name sentinel-kv --name dd-app-key --value "..."
   az keyvault secret set --vault-name sentinel-kv --name teams-webhook-url --value "https://..."
   az keyvault secret set --vault-name sentinel-kv --name langfuse-secret-key --value "sk-lf-..."
   az keyvault secret set --vault-name sentinel-kv --name langfuse-public-key --value "pk-lf-..."
   ```

8. [ ] Create Entra DB roles: connect to PostgreSQL as the group admin (token as
   password) and map the SP + backend UAMI to DB roles with grants:
   ```sql
   SELECT * FROM pgaadauth_create_principal('sentinel-gha', false, false);
   SELECT * FROM pgaadauth_create_principal('sentinel-backend-wi', false, false);
   -- then GRANT the needed privileges on the sentinel DB to each role
   ```

9. [ ] Build and push first CI runner image manually (§6.2)

10. [ ] Verify AKS + workload identity: `az aks get-credentials --resource-group sentinel-rg --name sentinel-aks && kubectl get nodes`
    (Terraform already enabled `oidc_issuer_enabled` + `workload_identity_enabled`.)

11. [ ] Run `alembic upgrade head` against PostgreSQL (Entra token auth) to create tables

### After bootstrap — ongoing

- Push to main on sentinel-infra → `ci_infra.yml` runs `terraform apply`
- Change `ci-images/` → `build-runners.yml` rebuilds runner image
- Terraform auto-distributes secrets to sentinel + sentinel-deployment repos
- Runtime secrets updated via `az keyvault secret set` (no Terraform needed)

---

## 11. Cost Breakdown

| Resource | Monthly Cost | Covered By |
|----------|-------------|------------|
| AKS control plane | Free | Always free (Free tier) |
| AKS node (1× B2ats_v2) | Free | Scale-to-zero — ~20-80 hrs/mo consumed of the free 750 (12-month, expires 05/2027) |
| LoadBalancer public IP | ~$0 | Released at teardown (Service deleted per run, URL resolved fresh by backend-up); bills only while scaled up |
| PostgreSQL B1MS | Free | 12-month free |
| ACR Standard | Free | 12-month free (100 GB) |
| Key Vault | Free | Always free |
| Event Grid | Free | Always free (100K ops) — custom topic + KV system topic |
| Azure Functions | Free | Always free (1M reqs) — bridge + rotator share the Y1 plan |
| App Service F1 | Free | Always free |
| Storage (TF state) | ~$0.01 | Negligible |
| **Total** | **~$0/month at idle** | **12 months** |

Backend compute = the free AKS node, scaled to zero between runs (sentinel repo
workflows manage node_count — Terraform ignores drift on it). After 05/2027 the node
bills only for scaled-up hours — `terraform destroy` when the project wraps.
