# sentinel-infra

Terraform IaC that provisions all Azure resources for the Sentinel system. Also builds CI runner images and stores them in ACR.

## What This Repo Will Contain

- Terraform modules for every Azure resource (6 modules)
- CI runner Dockerfiles (stored in ACR)
- CI pipeline: `terraform plan` on PR, `terraform apply` on merge

No K8s manifests — the backend runs as an ephemeral Docker container inside the GHA runner, not on AKS.

## Azure Resources Provisioned

| Resource | Module | Tier | Purpose |
|----------|--------|------|---------|
| Container Registry | `modules/acr/` | Standard (free 12 months) | Backend images + CI runner images |
| PostgreSQL | `modules/postgresql/` | B1MS (free 12 months) | Episodic + semantic memory with pgvector |
| Key Vault | `modules/keyvault/` | Always free | All secrets (API keys, DB password, etc.) |
| Event Grid | `modules/event-grid/` | Always free | Routes Datadog webhooks |
| Azure Functions | `modules/functions/` | Consumption (always free) | Event Grid → GHA repository_dispatch bridge |
| App Service | `modules/app-service/` | F1 (always free) | sentinel-deployment target (dummy-api) |

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| IaC tool | Terraform | Industry standard, mature Azure provider, interview value |
| Module structure | Per-resource modules (6) | Clean boundaries, independently testable |
| State backend | Azure Storage | Native locking, no DynamoDB needed |
| CI runner images | Custom Docker in ACR | Eliminates per-run tool installs |
| No AKS | Backend is ephemeral | Runs inside GHA runner per incident — zero compute cost |
| LLM provider secrets | Anthropic + OpenAI keys in Key Vault | Fetched by GHA at incident start, passed to ephemeral container |

## Key Vault Secrets

| Secret | Purpose |
|--------|---------|
| `anthropic-api-key` | Default LLM provider |
| `openai-api-key` | Fallback LLM provider |
| `db-password` | PostgreSQL admin password |
| `dd-api-key` | Datadog API key |
| `teams-webhook-url` | Teams incoming webhook |
| `langfuse-secret-key` | LangFuse tracing |
| `langfuse-public-key` | LangFuse tracing |
| `acr-password` | ACR admin password (for GHA docker pull) |
| `azure-sp-secret` | Service principal for sentinel-deployment GHA |

## Key Docs

| Doc | Purpose |
|-----|---------|
| `ARCHITECTURE.md` | Full Terraform module breakdown, state management, CI pipeline |
| `../reference-documentation/links.md` | Azure free tier limits and service docs |
| `../../Subscription-plans/azure-reference.md` | Azure Student account details |

## Cost Target

$0/month — everything within free tier or 12-month free allowances. $139 Azure credit untouched (except ~$0.01 for TF state storage). No compute costs (backend is ephemeral inside GHA runner).

## Status

Architecture doc written. Waiting on Azure resource group creation (manual bootstrap) to start implementation.
