# sentinel-infra

Terraform IaC that provisions all Azure resources for the Sentinel system. Single `terraform apply` brings up the entire stack. Also manages CI runner images, OIDC federation, and cross-repo secret distribution.

## What This Repo Contains

- 7 Terraform modules (AKS, ACR, PostgreSQL, Key Vault, Event Grid, Functions, App Service)
- OIDC federated credentials for all three repos (no stored client secrets)
- Cross-repo secret distribution via GitHub provider (ACR creds + OIDC IDs pushed automatically)
- CI runner Dockerfiles (stored in ACR)
- CI/CD: `ci_infra_dry.yml` (validate + plan), `ci_infra.yml` (apply on merge), `ci_runners.yml` (build runner images)

Terraform provisions the AKS cluster only — the backend's K8s manifests live in the sentinel repo (`azure/k8s/`) and are applied by its `ci_backend_deployment.yml`.

## Azure Resources Provisioned

| Resource | Module | Tier | Purpose |
|----------|--------|------|---------|
| AKS | `modules/aks/` | Free control plane + 1× B2ats_v2 node (free 12 months) | Hosts sentinel-backend (single replica, public LB IP, node pool scaled 0↔1 per run by sentinel CI) |
| Container Registry | `modules/acr/` | Standard (free 12 months) | Backend images + CI runner images |
| PostgreSQL | `modules/postgresql/` | B1MS (free 12 months) | Episodic + semantic memory with pgvector |
| Key Vault | `modules/keyvault/` | Always free | All runtime secrets + RBAC for GHA access |
| Event Grid | `modules/event-grid/` | Always free | Routes Datadog webhooks |
| Azure Functions | `modules/functions/` | Consumption (always free) | Event Grid → GHA repository_dispatch bridge |
| App Service | `modules/app-service/` | F1 (always free) | sentinel-deployment target (dummy-api) |

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| IaC tool | Terraform | Industry standard, interview value, PR-reviewable |
| Auth | OIDC (workload identity federation) | No stored secrets to rotate — GitHub proves identity via JWT |
| Module structure | Per-resource modules (7) | Clean boundaries, independently testable |
| Backend hosting | AKS, single replica, scale-to-zero | Terraform provisions the cluster (ignores node_count drift); sentinel CI deploys the app and scales the pool 0↔1 per run |
| State backend | Azure Storage | Native locking, no DynamoDB needed |
| Secret distribution | `github_actions_secret` Terraform resource | ACR creds + OIDC IDs pushed to sentinel + sentinel-deployment automatically |
| Key Vault access | Two roles: Officer (Terraform), User (GHA) | Terraform writes secrets, GHA only reads them at runtime |
| PostgreSQL firewall | Allow all (dev) | GHA runners (deploy recording, context fetches) have dynamic IPs outside Azure range; AKS egress alone isn't enough |
| CI runner images | Custom Docker in ACR | Eliminates per-run tool installs |

## GitHub Secrets (this repo)

| Secret | Description |
|--------|-------------|
| `AZURE_CLIENT_ID` | OIDC app client ID |
| `AZURE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID |
| `DB_PASSWORD` | PostgreSQL admin password |
| `GITHUB_PAT` | GitHub PAT for cross-repo secret distribution |

No `AZURE_CLIENT_SECRET` — OIDC eliminates it.

## Key Docs

| Doc | Purpose |
|-----|---------|
| `ARCHITECTURE.md` | Full module breakdown, OIDC setup, secret flow, CI/CD, bootstrap checklist |
| `../reference-documentation/links.md` | Azure free tier limits and service docs |

## Cost Target

~$0/month at idle — everything within free tier or 12-month free allowances. AKS node runs scale-to-zero: ~20-80 of the free 750 B2ats_v2 hrs/mo consumed, the rest stays available for other projects. The LoadBalancer IP is released at teardown (Service deleted per run), so nothing bills while Sentinel is down.

## Status

Architecture finalized. Waiting on Azure resource group creation (manual bootstrap) to start implementation.
