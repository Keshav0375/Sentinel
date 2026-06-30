# sentinel-infra

Planning for the **sentinel-infra** repo — Terraform IaC that provisions all Azure resources Sentinel depends on.

## What This Repo Will Contain

- Terraform modules for every Azure resource
- Environment configs (dev only for now)
- CI pipeline for `terraform plan` on PR, `terraform apply` on merge

## Azure Resources to Provision

| Resource | Azure Service | Tier | Purpose |
|----------|--------------|------|---------|
| Backend compute | App Service or AKS | Free control plane / F1 | Hosts the Sentinel backend API |
| Database | Azure Database for PostgreSQL | B1MS (free 12 months) | Episodic + semantic memory |
| Event routing | Event Grid | Always free (100K ops) | Routes Datadog/GitHub webhooks to the right handler |
| Webhook bridge | Azure Functions | Always free (1M reqs) | Receives Event Grid events → fires GHA `repository_dispatch` |
| Container registry | Azure Container Registry | Standard (free 12 months) | Stores backend Docker images |
| Secrets | Key Vault | Always free (10K txns) | API keys, service principal creds |

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| IaC tool | Terraform | Industry standard, Azure provider mature |
| Module structure | TBD | Open decision — monorepo vs per-resource |

## Cost Target

$0/month — everything within free tier or 12-month free allowances. $139 Azure credit untouched.

## Key Docs

| Doc | Purpose |
|-----|---------|
| `ARCHITECTURE.md` | (to be written) Full Terraform module breakdown, state management, CI pipeline |
| `../reference-documentation/links.md` | Azure free tier limits and service docs |
| `../../Subscription-plans/azure-reference.md` | Azure Student account status and all free tier details |

## Status

Planning not started. Depends on sentinel-deployment and sentinel-backend architecture being finalized first.
