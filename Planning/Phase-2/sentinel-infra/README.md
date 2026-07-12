# sentinel-infra

Terraform IaC that provisions all Azure resources for the Sentinel system. Single `terraform apply` brings up the entire stack. Also manages CI runner images, OIDC federation, and cross-repo secret distribution.

## What This Repo Contains

- 7 Terraform modules (AKS, ACR, PostgreSQL, Key Vault, Event Grid, Functions, App Service)
- **Identity plane:** OIDC federated credentials (all three repos), the backend Entra app registration (`api://sentinel-backend`), AKS workload identity (backend UAMI + federated credential)
- **Entra-only PostgreSQL** (no password) + **Key Vault secret rotation** (rotator Function)
- Cross-repo secret/variable distribution via GitHub provider (ACR creds + identity pointers pushed automatically)
- CI runner Dockerfiles (stored in ACR)
- CI/CD: `ci_infra_dry.yml` (validate + plan), `ci_infra.yml` (apply on merge), `ci_destroy_infra.yml` (manual full teardown), `ci_runners.yml` (build runner images)

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
| GHA → Azure auth | OIDC (workload identity federation) | No stored client secret — GitHub proves identity via JWT |
| Backend API auth | Entra app `api://sentinel-backend` + `Incident.Write` role | Inbound bearer tokens validated vs JWKS — deletes the shared `sentinel-api-token` |
| PostgreSQL auth | **Entra-only** (no password) | Clients present a short-lived Entra token; `db-password` eliminated |
| Backend → Azure auth | AKS **workload identity** (UAMI + federated credential) | Pod reads Key Vault + gets DB token with no stored secret |
| Secret rotation | Key Vault rotation policy + rotator Function | LLM keys rotate on `SecretNearExpiry`; not HashiCorp Vault (Azure has no dynamic engine) |
| Module structure | Per-resource modules (7) + identity plane | Clean boundaries, independently testable |
| Backend hosting | AKS, single replica, scale-to-zero | Terraform provisions the cluster (ignores node_count drift); sentinel CI deploys the app and scales the pool 0↔1 per run |
| Teardown | `ci_destroy_infra` — "everything, always" | Manual full teardown: `terraform destroy` + `az group delete`; restart re-runs bootstrap |
| State backend | Azure Storage | Native locking, no DynamoDB needed |
| Secret distribution | `github_actions_secret` / `_variable` Terraform resource | ACR creds + identity pointers pushed to sentinel + sentinel-deployment automatically |
| Key Vault access | Officer (Terraform + rotator), User (GHA + backend UAMI) | Setup + rotation write; GHA + pod read at runtime |
| Event Grid | Two Datadog signal types → bridge stamps `signal_type` | deploy_failure → rollback; runtime_error → full incident response |
| PostgreSQL firewall | Allow all (dev) | Network reach ≠ auth — Entra token still required; GHA runner IPs are dynamic |
| CI runner images | Custom Docker in ACR | Eliminates per-run tool installs |
| GitHub identity | Owner `Keshav0375`, repos `Sentinel-infra` / `Sentinel-deployment` / `Sentinel` | OIDC `sub` claims are exact case-sensitive matches — real owner/casing required (reconciled 2026-07-11) |

## GitHub Config (this repo)

**Variables (non-secret — just identifiers):** `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
`AZURE_SUBSCRIPTION_ID`, `PG_ADMIN_GROUP_OBJECT_ID` (the `sentinel-db-admins` group).

**Secrets (genuine credentials):** `GITHUB_PAT` (cross-repo distribution + Function bridge).

No `AZURE_CLIENT_SECRET` (OIDC), no `DB_PASSWORD` (Entra DB auth), no
`sentinel-api-token` (Entra bearer). Only the PAT + `ACR_*` (pushed to consumer repos)
remain as real secrets.

## Key Docs

| Doc | Purpose |
|-----|---------|
| `ARCHITECTURE.md` | Full module breakdown, OIDC setup, secret flow, CI/CD, bootstrap checklist |
| `../reference-documentation/links.md` | Azure free tier limits and service docs |

## Cost Target

~$0/month at idle — everything within free tier or 12-month free allowances. AKS node runs scale-to-zero: ~20-80 of the free 750 B2ats_v2 hrs/mo consumed, the rest stays available for other projects. The LoadBalancer IP is released at teardown (Service deleted per run), so nothing bills while Sentinel is down.

## Status

Architecture finalized (**rev 4 — 2026-07-12**): Entra-only PostgreSQL, Key Vault
rotation Function, backend Entra app registration, AKS workload identity, Event Grid
two-signal routing, and `ci_destroy_infra` full-teardown workflow. GitHub owner/repo
identity is `Keshav0375` + real repo casing (2026-07-11). Waiting on Azure resource
group + Entra `sentinel-db-admins` group creation (manual bootstrap) to start
implementation.
