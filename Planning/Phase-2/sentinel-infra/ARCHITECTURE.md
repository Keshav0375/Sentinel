# sentinel-infra — Architecture Document

> **Purpose:** Terraform IaC that provisions all Azure resources Sentinel depends on.
> Single `terraform apply` brings up the entire stack. Also manages CI runner
> images in ACR and cross-repo secret distribution.
>
> **Note:** No AKS — the backend runs as an ephemeral Docker container inside the
> GHA runner per incident. Only always-on resources are provisioned here.

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
│  │  Images:                │    │  ├── anthropic-api-key        │ │
│  │  ├── sentinel-backend   │    │  ├── openai-api-key           │ │
│  │  └── ci-runner          │    │  ├── db-password              │ │
│  └─────────────────────────┘    │  ├── dd-api-key               │ │
│                                  │  ├── teams-webhook-url        │ │
│  ┌─────────────────────────┐    │  ├── langfuse-secret-key      │ │
│  │  PostgreSQL B1MS        │    │  ├── langfuse-public-key      │ │
│  │  (free 12 months)       │    │  ├── acr-password              │ │
│  │                         │    │  └── github-pat                │ │
│  │  DB: sentinel           │    └──────────────────────────────┘ │
│  │  Extension: pgvector    │                                      │
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
└──────────────────────────────────────────────────────────────────┘

Backend hosting: NOT here. sentinel-backend runs as an ephemeral
Docker container inside the GHA runner during ci_incident_response.yml.
Image pulled from ACR on demand. Zero always-on compute cost.
```

---

## 2. Terraform Module Structure

One module per Azure resource group concern. Flat structure — no nested modules.
6 modules total (AKS removed — backend is ephemeral).

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
│   ├── functions/             # Azure Function App (Event Grid → GHA bridge)
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   └── src/               # Function source code (Python)
│   │       └── bridge/
│   │           ├── __init__.py
│   │           └── function.json
│   │
│   └── app-service/           # App Service F1 for sentinel-deployment
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
| `sentinel-backend:sha-X` | Backend API container (pulled by GHA runner) | ci_backend_validation.yml (sentinel repo) |
| `sentinel-backend:latest` | Latest built version | ci_backend_validation.yml |
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
  administrator_login    = "sentinel_admin"
  administrator_password = var.db_password
  zone                   = "1"

  authentication {
    password_auth_enabled = true
  }
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

# Firewall: allow all (dev)
# GitHub-hosted runners have dynamic IPs outside Azure's service range.
# The ephemeral backend container runs ON the GHA runner, so it also
# connects from a GitHub IP, not an Azure IP. During dev we allow all.
# Production would use Private Endpoint + VNet integration.
resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_all_dev" {
  name             = "allow-all-dev"
  server_id        = azurerm_postgresql_flexible_server.sentinel.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "255.255.255.255"
}
```

**Why allow-all?** The ephemeral backend runs inside a GitHub-hosted runner. GitHub
rotates runner IPs across a wide CIDR range that changes weekly. There's no stable
IP to whitelist. The `0.0.0.0/0.0.0.0` rule (Azure services only) doesn't cover
GitHub runners — they're not Azure services. Options:

| Approach | Dev? | Production? |
|----------|------|-------------|
| Allow all (`0.0.0.0`–`255.255.255.255`) | **Yes — simple, works** | No |
| GitHub meta API IP ranges (dynamic) | Fragile — IPs change | No |
| Azure-hosted self-hosted runner | Works but costs money | Maybe |
| Private Endpoint + VNet | Overkill for dev | **Yes** |

We use allow-all for dev. The DB is still protected by username + password.
Lock down later if needed.

**Outputs:** `db_host`, `db_name`, `db_port`

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
```

**Two access levels:**

| Identity | Role | Purpose |
|----------|------|---------|
| Terraform SP | `Key Vault Secrets Officer` | Create/update secrets during `terraform apply` |
| GHA SP (OIDC) | `Key Vault Secrets User` | Read-only — `az keyvault secret show` in ci_incident_response.yml |

The GHA SP is the same OIDC-federated identity used by all three repos'
workflows. It can read secrets but never modify them — only Terraform can
write secrets.

**Secrets stored:**

| Secret Name | Description | Consumed By |
|-------------|-------------|-------------|
| `anthropic-api-key` | Anthropic LLM provider (default) | Backend (env var `ANTHROPIC_API_KEY`) |
| `openai-api-key` | OpenAI LLM provider (fallback) | Backend (env var `OPENAI_API_KEY`) |
| `db-password` | PostgreSQL admin password | Backend (env var `DATABASE_URL`) |
| `dd-api-key` | Datadog API key | GHA jobs (Datadog event reporting) |
| `dd-app-key` | Datadog app key | GHA jobs (Datadog log queries) |
| `teams-webhook-url` | Teams incoming webhook | GHA jobs (notifications) |
| `langfuse-secret-key` | LangFuse tracing (secret) | Backend (env var `LANGFUSE_SECRET_KEY`) |
| `langfuse-public-key` | LangFuse tracing (public) | Backend (env var `LANGFUSE_PUBLIC_KEY`) |
| `acr-password` | ACR admin password | GHA jobs (docker pull/push) |
| `github-pat` | GitHub PAT with `repo` scope | Azure Function bridge (repository_dispatch) |

**Secret flow: Key Vault → GHA → ephemeral backend:**

```
ci_incident_response.yml
│
├── fetch-secrets job:
│   az keyvault secret show --vault-name sentinel-kv --name anthropic-api-key
│   az keyvault secret show --vault-name sentinel-kv --name openai-api-key
│   az keyvault secret show --vault-name sentinel-kv --name db-password
│   az keyvault secret show --vault-name sentinel-kv --name langfuse-secret-key
│   az keyvault secret show --vault-name sentinel-kv --name langfuse-public-key
│   → sets as job outputs (masked)
│
├── start-backend job:
│   docker run -d \
│     -e ANTHROPIC_API_KEY=${{ needs.fetch-secrets.outputs.anthropic-api-key }} \
│     -e OPENAI_API_KEY=${{ needs.fetch-secrets.outputs.openai-api-key }} \
│     -e DATABASE_URL=postgresql://sentinel_admin:${{ needs.fetch-secrets.outputs.db-password }}@sentinel-pg.postgres.database.azure.com/sentinel \
│     -e LANGFUSE_SECRET_KEY=${{ needs.fetch-secrets.outputs.langfuse-secret-key }} \
│     -e LANGFUSE_PUBLIC_KEY=${{ needs.fetch-secrets.outputs.langfuse-public-key }} \
│     -e SENTINEL_PRIMARY_PROVIDER=anthropic \
│     -p 8000:8000 \
│     sentinelacr.azurecr.io/sentinel-backend:latest
│
│   → validate: curl http://localhost:8000/health
│   → validate: curl http://localhost:8000/ready  (checks DB + LangFuse)
│
├── ... pipeline jobs use http://localhost:8000 ...
│
└── teardown-backend job:
    docker stop sentinel-backend && docker rm sentinel-backend
```

### 3.4 Event Grid Module

```hcl
resource "azurerm_eventgrid_topic" "sentinel" {
  name                = "sentinel-events"
  location            = var.location
  resource_group_name = var.resource_group_name
}

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
    "GITHUB_REPO"       = "keshxvDev/sentinel"
    "GITHUB_EVENT_TYPE" = "incident-alert"
  }
}
```

**Bridge function source (Python):**

```python
import json
import httpx
import azure.functions as func

def main(event: func.EventGridEvent):
    """Event Grid → GitHub repository_dispatch"""
    data = event.get_json()

    httpx.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/dispatches",
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        },
        json={
            "event_type": GITHUB_EVENT_TYPE,
            "client_payload": data,
        },
    )
```

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

---

## 4. OIDC Authentication (Workload Identity Federation)

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
  subject        = "repo:keshxvDev/sentinel-infra:ref:refs/heads/main"
}

resource "azuread_application_federated_identity_credential" "sentinel_infra_pr" {
  application_id = azuread_application.sentinel_gha.id
  display_name   = "sentinel-infra-pr"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:keshxvDev/sentinel-infra:pull_request"
}

resource "azuread_application_federated_identity_credential" "sentinel_main" {
  application_id = azuread_application.sentinel_gha.id
  display_name   = "sentinel-main"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:keshxvDev/sentinel:ref:refs/heads/main"
}

resource "azuread_application_federated_identity_credential" "sentinel_pr" {
  application_id = azuread_application.sentinel_gha.id
  display_name   = "sentinel-pr"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:keshxvDev/sentinel:pull_request"
}

resource "azuread_application_federated_identity_credential" "sentinel_deployment_main" {
  application_id = azuread_application.sentinel_gha.id
  display_name   = "sentinel-deployment-main"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:keshxvDev/sentinel-deployment:ref:refs/heads/main"
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
  "subject": "repo:keshxvDev/sentinel-infra:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'

# Also for PR (so ci_infra.yml can run terraform plan)
az ad app federated-credential create --id $APP_ID --parameters '{
  "name": "sentinel-infra-pr",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:keshxvDev/sentinel-infra:pull_request",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

After this, add `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
to sentinel-infra GitHub repo secrets. Then `ci_infra.yml` can run and Terraform
manages all remaining federated credentials for the other repos.

---

## 5. Cross-Repo Secret Distribution

After `terraform apply`, the sentinel and sentinel-deployment repos need ACR
credentials and other values as GitHub Actions secrets. Terraform pushes these
automatically using the GitHub provider — no manual copy-paste.

### 5.1 GitHub provider config

```hcl
provider "github" {
  token = var.github_pat
  owner = "keshxvDev"
}
```

### 5.2 Secrets pushed to sentinel repo

```hcl
resource "github_actions_secret" "sentinel_acr_login_server" {
  repository      = "sentinel"
  secret_name     = "ACR_LOGIN_SERVER"
  plaintext_value = azurerm_container_registry.sentinel.login_server
}

resource "github_actions_secret" "sentinel_acr_username" {
  repository      = "sentinel"
  secret_name     = "ACR_USERNAME"
  plaintext_value = azurerm_container_registry.sentinel.admin_username
}

resource "github_actions_secret" "sentinel_acr_password" {
  repository      = "sentinel"
  secret_name     = "ACR_PASSWORD"
  plaintext_value = azurerm_container_registry.sentinel.admin_password
}

resource "github_actions_secret" "sentinel_azure_client_id" {
  repository      = "sentinel"
  secret_name     = "AZURE_CLIENT_ID"
  plaintext_value = azuread_application.sentinel_gha.client_id
}

resource "github_actions_secret" "sentinel_azure_tenant_id" {
  repository      = "sentinel"
  secret_name     = "AZURE_TENANT_ID"
  plaintext_value = data.azurerm_client_config.current.tenant_id
}

resource "github_actions_secret" "sentinel_azure_subscription_id" {
  repository      = "sentinel"
  secret_name     = "AZURE_SUBSCRIPTION_ID"
  plaintext_value = data.azurerm_client_config.current.subscription_id
}
```

### 5.3 Secrets pushed to sentinel-deployment repo

```hcl
resource "github_actions_secret" "deployment_azure_client_id" {
  repository      = "sentinel-deployment"
  secret_name     = "AZURE_CLIENT_ID"
  plaintext_value = azuread_application.sentinel_gha.client_id
}

resource "github_actions_secret" "deployment_azure_tenant_id" {
  repository      = "sentinel-deployment"
  secret_name     = "AZURE_TENANT_ID"
  plaintext_value = data.azurerm_client_config.current.tenant_id
}

resource "github_actions_secret" "deployment_azure_subscription_id" {
  repository      = "sentinel-deployment"
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

**Two layers of secrets:**
- **GitHub repo secrets** = identity (who am I?) — OIDC credentials, ACR access
- **Key Vault secrets** = runtime values (what do I need?) — API keys, DB password, webhook URLs

GitHub secrets are set once by Terraform and rarely change. Key Vault secrets
can be updated independently via `az keyvault secret set` without re-running Terraform.

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
# In sentinel repo's ci_validation.yml / ci_backend_validation.yml:
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
        run: terraform plan -var="db_password=${{ secrets.DB_PASSWORD }}" -var="github_pat=${{ secrets.GITHUB_PAT }}" -no-color -out=tfplan

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
        run: terraform apply -auto-approve -var="db_password=${{ secrets.DB_PASSWORD }}" -var="github_pat=${{ secrets.GITHUB_PAT }}"
```

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

| Secret | Description | How Set |
|--------|-------------|---------|
| `AZURE_CLIENT_ID` | OIDC app client ID | Manual (from bootstrap §4.3) |
| `AZURE_TENANT_ID` | Azure AD tenant ID | Manual |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID | Manual |
| `DB_PASSWORD` | PostgreSQL admin password | Manual (you choose this) |
| `GITHUB_PAT` | GitHub PAT with `repo` scope | Manual (for github_actions_secret provider) |

**No `AZURE_CLIENT_SECRET`** — OIDC eliminates it entirely.

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

3. [ ] Create OIDC service principal + federated credentials (§4.3)

4. [ ] Add 5 secrets to sentinel-infra GitHub repo (§9)

5. [ ] First `terraform apply` (can be local or via GHA):
   ```bash
   terraform init
   terraform apply -var="db_password=<your-password>" -var="github_pat=<your-pat>"
   ```

6. [ ] Populate Key Vault with runtime secrets:
   ```bash
   az keyvault secret set --vault-name sentinel-kv --name anthropic-api-key --value "sk-ant-..."
   az keyvault secret set --vault-name sentinel-kv --name openai-api-key --value "sk-..."
   az keyvault secret set --vault-name sentinel-kv --name dd-api-key --value "..."
   az keyvault secret set --vault-name sentinel-kv --name dd-app-key --value "..."
   az keyvault secret set --vault-name sentinel-kv --name teams-webhook-url --value "https://..."
   az keyvault secret set --vault-name sentinel-kv --name langfuse-secret-key --value "sk-lf-..."
   az keyvault secret set --vault-name sentinel-kv --name langfuse-public-key --value "pk-lf-..."
   ```

7. [ ] Build and push first CI runner image manually (§6.2)

8. [ ] Verify PostgreSQL: `psql` connection test from local machine

9. [ ] Run `alembic upgrade head` against PostgreSQL to create tables

### After bootstrap — ongoing

- Push to main on sentinel-infra → `ci_infra.yml` runs `terraform apply`
- Change `ci-images/` → `build-runners.yml` rebuilds runner image
- Terraform auto-distributes secrets to sentinel + sentinel-deployment repos
- Runtime secrets updated via `az keyvault secret set` (no Terraform needed)

---

## 11. Cost Breakdown

| Resource | Monthly Cost | Covered By |
|----------|-------------|------------|
| PostgreSQL B1MS | Free | 12-month free |
| ACR Standard | Free | 12-month free (100 GB) |
| Key Vault | Free | Always free |
| Event Grid | Free | Always free (100K ops) |
| Azure Functions | Free | Always free (1M reqs) |
| App Service F1 | Free | Always free |
| Storage (TF state) | ~$0.01 | Negligible |
| **Total** | **~$0/month** | **12 months** |

No compute costs — backend runs ephemerally inside GHA runner (free minutes).
