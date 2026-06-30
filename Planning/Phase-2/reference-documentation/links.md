# Phase 2 — Reference Documentation & Links

---

## Datadog × Azure Integration

### Primary Docs

| Doc | URL | What It Covers |
|-----|-----|----------------|
| Azure Integration (main) | https://docs.datadoghq.com/integrations/azure/ | Top-level setup, app registration, supported services |
| Azure App Service Integration | https://docs.datadoghq.com/integrations/azure_app_services/ | Metrics auto-collection for App Service (CPU, memory, HTTP codes, response time) |
| Azure Portal Setup Guide | https://docs.datadoghq.com/integrations/guide/azure-portal/ | Step-by-step: link existing DD org to Azure subscription via OAuth |
| Azure Log Forwarding | https://docs.datadoghq.com/logs/guide/azure-logging-guide/ | Automated log forwarding via ARM templates, diagnostic settings, storage accounts |
| Datadog Events API | https://docs.datadoghq.com/api/latest/events/ | POST events from GHA pipeline (`/api/v1/events`) |
| Datadog Log Intake API | https://docs.datadoghq.com/api/latest/logs/ | POST structured logs from GHA pipeline (`/api/v2/logs`) |

### Setup Flow (for Sentinel Phase 2)

**Two parallel paths for getting data into Datadog:**

1. **Native Azure Integration (metrics + platform logs)**
   - Register an Azure AD app (App Registration) with `Monitoring Reader` role on the subscription
   - In Datadog UI → Integrations → Azure → add the app's `tenant_id`, `client_id`, `client_secret`
   - Datadog auto-discovers Azure resources and pulls metrics every ~2 minutes
   - For App Service: CPU time, memory working set, HTTP status codes, response time, bytes in/out — all collected automatically, no agent needed
   - For logs: enable Diagnostic Settings on the App Service → route to a Storage Account → Datadog's Azure Function forwarder picks them up
   - ARM template available for automated log forwarding infrastructure deployment

2. **GHA Pipeline → Datadog API (deploy events + structured logs)**
   - This is what `deploy.yml` does — `curl` calls to Events API and Log Intake API
   - Not part of the native integration — these are custom events/logs we create
   - Requires only `DD_API_KEY` as a GitHub secret
   - Events land in Datadog Events Explorer (timeline markers)
   - Logs land in Datadog Log Explorer (searchable, filterable)

### Key Findings from Docs

**What the native integration gives us for free (App Service):**

```
METRIC                              | DESCRIPTION
-------------------------------------|--------------------------------------------
azure.app_service_plan.cpu_percentage | CPU % across the App Service Plan
azure.app_service_plan.memory_percentage | Memory % across the Plan
azure.app_service.requests            | Total request count
azure.app_service.http_2xx            | 2xx response count
azure.app_service.http_4xx            | 4xx response count
azure.app_service.http_5xx            | 5xx response count
azure.app_service.response_time       | Average response time (seconds)
azure.app_service.bytes_received      | Inbound bytes
azure.app_service.bytes_sent          | Outbound bytes
azure.app_service.cpu_time            | CPU time consumed (seconds)
azure.app_service.memory_working_set  | Memory working set (bytes)
azure.app_service.health_check_status | Health check pass/fail
```

These metrics appear automatically once the Azure integration is configured — no code changes to the app, no Datadog agent install.

**What we add via GHA pipeline (custom):**

```
SOURCE          | TYPE  | CONTENT
-----------------|-------|------------------------------------------
deploy.yml       | Event | "Deploy succeeded/failed for PR #N"
deploy.yml       | Log   | Structured JSON: stage results, version, PR metadata
```

**Log forwarding architecture:**
- Diagnostic Settings → Storage Account → Azure Function (forwarder) → Datadog
- ARM template auto-deploys the Function + Storage Account
- Only supports Azure commercial cloud (not gov/China)
- Storage accounts deployed per-subscription

**Setup methods:**
1. Azure Portal: Datadog Service → Link existing org → OAuth flow
2. Terraform: `azurerm_datadog_monitor` resource + `Monitoring Reader` role assignment
3. Manual: App Registration → grant Reader role → paste credentials in DD UI

**Limitations:**
- Azure Government and China clouds not supported for automated log forwarding
- Datadog free tier (Student Pack): Pro account, 10 servers, 2 years — more than enough
- App Service free tier (F1): 60 min CPU/day, 1 GB RAM, 1 GB storage — sufficient for dummy-api
- Native metrics are polled every ~2 minutes (not real-time)

---

## Azure App Service (Free Tier — F1)

### Docs

| Doc | URL |
|-----|-----|
| App Service Pricing | https://azure.microsoft.com/en-us/pricing/details/app-service/linux/ |
| Quickstart: Deploy Python App | https://learn.microsoft.com/en-us/azure/app-service/quickstart-python |
| GitHub Actions Deploy to App Service | https://learn.microsoft.com/en-us/azure/app-service/deploy-github-actions |
| App Service Free Tier Limits | https://azure.microsoft.com/en-us/pricing/details/app-service/windows/ |

### Free Tier (F1) Limits

```
CPU          : 60 min/day (shared compute)
Memory       : 1 GB RAM
Storage      : 1 GB
Custom domain: No (uses *.azurewebsites.net)
SSL          : No custom SSL (Azure-provided HTTPS works)
Always On    : No (app sleeps after idle)
Slots        : 0 (no staging slots)
Scale out    : Not available
```

For dummy-api (3 endpoints, no background work), F1 is more than sufficient. The app will sleep after idle, but wakes on request — fine for our use case since we only hit it during deploy verification.

### Deploy from GHA to App Service

Azure provides a first-party GitHub Action: `azure/webapps-deploy@v3`

```yaml
- uses: azure/login@v2
  with:
    creds: ${{ secrets.AZURE_CREDENTIALS }}

- uses: azure/webapps-deploy@v3
  with:
    app-name: 'dummy-api'
    package: '.'
```

**Required GitHub Secret:** `AZURE_CREDENTIALS` — a service principal JSON blob:
```json
{
  "clientId": "<app-id>",
  "clientSecret": "<secret>",
  "subscriptionId": "<sub-id>",
  "tenantId": "<tenant-id>"
}
```

Create via: `az ad sp create-for-rbac --name "sentinel-deploy-sp" --role contributor --scopes /subscriptions/<sub-id>/resourceGroups/<rg-name>`

---

## Datadog (Student Pack)

### Docs

| Doc | URL |
|-----|-----|
| Datadog Getting Started | https://docs.datadoghq.com/getting_started/ |
| Events API Reference | https://docs.datadoghq.com/api/latest/events/#post-an-event |
| Log Intake (HTTP) | https://docs.datadoghq.com/api/latest/logs/#send-logs |
| Datadog Sites | https://docs.datadoghq.com/getting_started/site/ |

### Student Pack Offer

```
Plan     : Datadog Pro
Servers  : Up to 10
Duration : 2 years (free)
Includes : APM, Logs, Infrastructure, Events, Dashboards
```

### API Endpoints We Use

```
Events API  : POST https://api.datadoghq.com/api/v1/events
Log Intake  : POST https://http-intake.logs.datadoghq.com/api/v2/logs
```

Note: The `datadoghq.com` domain is for US1 site. If your account lands on a different site (US3, US5, EU1), the domain changes (e.g., `us3.datadoghq.com`). Check your site after signup at Organization Settings → Datadog Site.

---

## GitHub Actions

### Docs

| Doc | URL |
|-----|-----|
| Deploy to Azure App Service | https://learn.microsoft.com/en-us/azure/app-service/deploy-github-actions |
| azure/login action | https://github.com/Azure/login |
| azure/webapps-deploy action | https://github.com/Azure/webapps-deploy |
| Workflow syntax reference | https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions |

### Budget

```
Quota    : 3,000 min/month (GitHub Pro via Student Pack)
Per run  : ~3 min (build + deploy + verify)
Estimate : ~20 merges/month = 60 min → 2% of quota
```

---

## Architecture Change: DigitalOcean → Azure App Service

**Why:** DigitalOcean requires a payment method even with the $200 student credit. Azure Student account is already active with $139 credit, and App Service F1 tier is always-free (no credit consumed).

**Impact on sentinel-deployment ARCHITECTURE.md:**
- Replace all DigitalOcean references (App Platform, Container Registry, `doctl`) with Azure equivalents
- Deploy target: Azure App Service (F1 free tier)
- Container registry: Azure Container Registry (free 100 GB for 12 months) — OR skip containers entirely and use App Service's native Python deployment (zip deploy)
- GHA actions: `azure/login@v2` + `azure/webapps-deploy@v3` instead of `doctl`
- Secrets: `AZURE_CREDENTIALS` instead of `DIGITALOCEAN_ACCESS_TOKEN`
