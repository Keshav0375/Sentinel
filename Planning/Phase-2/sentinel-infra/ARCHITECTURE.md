# sentinel-infra — Architecture Document

> **Purpose:** Terraform IaC that provisions all Azure resources Sentinel depends on.
> Also builds and stores CI runner images in ACR for the sentinel repo's backend
> and incident pipeline workflows. Single `terraform apply` brings up the entire stack.
>
> **Note:** No AKS — the backend runs as an ephemeral Docker container inside the
> GHA runner per incident. Only always-on resources are provisioned here.

---

## 1. System Overview

```
sentinel-infra repo
       │
       │  terraform apply
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
│  │  ├── ci-runner          │    │  ├── db-password              │ │
│  │  └── ci-runner:python   │    │  ├── dd-api-key               │ │
│  └─────────────────────────┘    │  ├── teams-webhook-url        │ │
│                                  │  ├── langfuse-secret-key      │ │
│  ┌─────────────────────────┐    │  ├── langfuse-public-key      │ │
│  │  PostgreSQL B1MS        │    │  ├── acr-password              │ │
│  │  (free 12 months)       │    │  └── azure-sp-secret          │ │
│  │                         │    └──────────────────────────────┘ │
│  │  DB: sentinel           │                                      │
│  │  Extension: pgvector    │                                      │
│  │  32 GB storage          │                                      │
│  └─────────────────────────┘                                      │
│                                                                   │
│  ┌─────────────────────────┐                                      │
│  │  Event Grid Topic       │                                      │
│  │  (always free)          │                                      │
│  │  100K ops/month         │                                      │
│  └─────────┬───────────────┘                                      │
│            │                                                       │
│            ▼                                                       │
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
Docker container inside the GHA runner during incident_response.yml.
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
│       ├── plan.yml           # terraform plan on PR
│       └── apply.yml          # terraform apply on merge to main
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
| `sentinel-backend:sha-X` | Backend API container (pulled by GHA runner) | cd_backend.yml (sentinel repo) |
| `sentinel-backend:latest` | Latest built version | cd_backend.yml |
| `ci-runner:latest` | CI runner with Python 3.12 + dev tools | sentinel-infra CI |

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

# Firewall: allow Azure services + local dev IP
resource "azurerm_postgresql_flexible_server_firewall_rule" "azure" {
  name      = "allow-azure"
  server_id = azurerm_postgresql_flexible_server.sentinel.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}
```

**Outputs:** `db_host`, `db_name`, `db_port`

### 3.3 Key Vault Module

```hcl
resource "azurerm_key_vault" "sentinel" {
  name                = "sentinel-kv"
  location            = var.location
  resource_group_name = var.resource_group_name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"  # Always free (10K txns)

  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = data.azurerm_client_config.current.object_id

    secret_permissions = ["Get", "List", "Set", "Delete"]
  }
}
```

**Secrets stored:**

| Secret Name | Description |
|-------------|-------------|
| `anthropic-api-key` | Anthropic LLM provider (default) |
| `openai-api-key` | OpenAI LLM provider (fallback) |
| `db-password` | PostgreSQL admin password |
| `dd-api-key` | Datadog API key |
| `teams-webhook-url` | Teams incoming webhook |
| `langfuse-secret-key` | LangFuse tracing (secret) |
| `langfuse-public-key` | LangFuse tracing (public) |
| `acr-password` | ACR admin password (for GHA docker pull) |
| `azure-sp-secret` | Service principal for sentinel-deployment GHA |

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

## 4. CI Runner Images

The sentinel repo's CI workflows need specific tools. Instead of installing them
every run (slow), we build a custom runner image and store it in ACR.

### 4.1 ci-runner.Dockerfile

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gcc libpq-dev git && rm -rf /var/lib/apt/lists/*

# Dev tools
RUN pip install --no-cache-dir \
    ruff pyright pytest pytest-asyncio \
    asyncpg pgvector alembic

# Azure CLI (for deploy workflows)
RUN curl -sL https://aka.ms/InstallAzureCLIDeb | bash
```

### 4.2 Build and push

```bash
#!/bin/bash
# ci-images/build-push.sh
ACR_NAME="${1:?Usage: build-push.sh <acr-name>}"

az acr login --name $ACR_NAME

docker build -f ci-images/ci-runner.Dockerfile -t $ACR_NAME.azurecr.io/ci-runner:latest .
docker push $ACR_NAME.azurecr.io/ci-runner:latest
```

### 4.3 Usage in sentinel workflows

```yaml
# In sentinel repo's ci_backend.yml:
jobs:
  quality:
    runs-on: ubuntu-latest
    container:
      image: ${{ secrets.ACR_NAME }}.azurecr.io/ci-runner:latest
      credentials:
        username: ${{ secrets.ACR_USERNAME }}
        password: ${{ secrets.ACR_PASSWORD }}
```

This eliminates per-run install time for ruff, pyright, pytest, az cli.

---

## 5. Terraform State

### 5.1 Remote State (Azure Storage)

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

### 5.2 State Locking

Azure Storage provides native state locking via blob leases. No DynamoDB needed.

---

## 6. CI/CD Workflows

### 6.1 `plan.yml` — terraform plan on PR

```yaml
name: Terraform Plan

on:
  pull_request:
    branches: [main]

jobs:
  plan:
    name: Plan
    runs-on: ubuntu-latest
    steps:
      - Checkout
      - Setup Terraform
      - az login (service principal)
      - terraform init
      - terraform validate
      - terraform plan -out=tfplan
      - Post plan output as PR comment
```

### 6.2 `apply.yml` — terraform apply on merge

```yaml
name: Terraform Apply

on:
  push:
    branches: [main]

jobs:
  apply:
    name: Apply
    runs-on: ubuntu-latest
    environment: production
    steps:
      - Checkout
      - Setup Terraform
      - az login
      - terraform init
      - terraform apply -auto-approve
```

### 6.3 `build-runners.yml` — Build CI runner images

```yaml
name: Build CI Runners

on:
  push:
    branches: [main]
    paths:
      - 'ci-images/**'

jobs:
  build:
    name: Build + Push Runner Images
    runs-on: ubuntu-latest
    steps:
      - Checkout
      - az login
      - az acr login
      - docker build + push ci-runner:latest
```

---

## 7. Required GitHub Secrets (sentinel-infra repo)

| Secret | Description |
|--------|-------------|
| `AZURE_CLIENT_ID` | Service principal for Terraform |
| `AZURE_CLIENT_SECRET` | Service principal secret |
| `AZURE_TENANT_ID` | Azure AD tenant |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription |
| `DB_PASSWORD` | PostgreSQL admin password (passed to Terraform) |

---

## 8. Prerequisites & Setup Checklist

### One-time Bootstrap (manual)
- [ ] Create Azure resource group: `az group create --name sentinel-rg --location eastus`
- [ ] Create state storage: storage account + container (see §5.1)
- [ ] Create service principal for Terraform: `az ad sp create-for-rbac --name sentinel-terraform-sp --role contributor --scopes /subscriptions/<sub-id>`
- [ ] Add secrets to sentinel-infra GitHub repo

### After first `terraform apply`
- [ ] Verify ACR: `az acr login --name sentinelacr`
- [ ] Verify PostgreSQL: `psql` connection test
- [ ] Build and push CI runner image
- [ ] Run `alembic upgrade head` against PostgreSQL to create tables

---

## 9. Cost Breakdown

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
