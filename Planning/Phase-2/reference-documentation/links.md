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

## Azure Identity — Entra Auth, Key Vault Rotation, Workload Identity (rev-5)

### Docs

| Doc | URL | What It Covers |
|-----|-----|----------------|
| Entra auth — PostgreSQL Flexible Server | https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-azure-ad-authentication | Token-based DB login, admin model, `pgaadauth_create_principal`, token lifetimes |
| Configure Entra auth — PostgreSQL | https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/how-to-configure-sign-in-azure-ad-authentication | Retrieve token, connect with token as password |
| Key Vault secret rotation tutorial | https://learn.microsoft.com/en-us/azure/key-vault/secrets/tutorial-rotation | Rotation policy + `SecretNearExpiry` → Event Grid → Function pattern |
| Key Vault autorotation overview | https://learn.microsoft.com/en-us/azure/key-vault/general/autorotation | Which asset types autorotate; single vs dual credential |
| AKS Workload Identity overview | https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview | OIDC issuer, federated credential, SA annotation, `DefaultAzureCredential` |
| Entra protected web API (validate token) | https://learn.microsoft.com/en-us/entra/identity-platform/scenario-protected-web-api-app-configuration | App registration, app roles, JWKS validation (aud/iss/roles) |

### Key Findings (what drives our architecture)

**PostgreSQL Entra auth (rev-5 change 3a):**
- Modes: PG-only, Entra-only, or both. We use **Entra-only** (`password_auth_enabled=false`).
- The **token is the password**: `az account get-access-token --resource https://ossrdbms-aad.database.windows.net` → pass as `PGPASSWORD`. Username = the Entra principal name (SP/UAMI/group).
- **Token lifetime:** ~1 h for user/SP, **~24 h for a system-assigned managed identity** — the backend refreshes before expiry.
- Terraform must ensure Entra auth is ON before creating Entra DB roles (explicit `depends_on`); Azure matches token→role by the principal's **object ID**, not name.
- Admin can be a **group** (`sentinel-db-admins`) — add/remove members centrally without touching the server. Non-admin roles created via `pgaadauth_create_principal(...)`.

**Key Vault rotation (rev-5 change 3b):**
- Key Vault has **no dynamic secret engine** (that's HashiCorp Vault). It stores static versions + a **rotation policy**.
- Pattern: rotation policy fires **`SecretNearExpiry`** (default 30 days before expiry; a short expiry fires within ~15 min) → **Event Grid system topic** → **Function App** (system MI with Secrets Officer) generates the new value, writes a **new secret version**, updates the provider. Single-credential rotation has a brief lag window; a two-set variant avoids it.
- For LLM keys there's no ephemeral-key API — the Function rotates by calling the provider's key-management API (Anthropic Admin API where available) or degrades to a Teams reminder.

**AKS Workload Identity (rev-5 change 3c):**
- Enable **`oidc_issuer_enabled` + `workload_identity_enabled`** on the cluster.
- Federate: **UAMI** ← `azurerm_federated_identity_credential` with `issuer = cluster oidc_issuer_url`, `subject = system:serviceaccount:<ns>:<sa>`, `audience = api://AzureADTokenExchange`.
- Pod opts in with label **`azure.workload.identity/use: "true"`** + a ServiceAccount annotated **`azure.workload.identity/client-id`**. Code uses `DefaultAzureCredential`/`WorkloadIdentityCredential`; scopes use v2 `<resource>/.default`. Max 20 federated creds per identity.

**Backend inbound Entra bearer (rev-5 change 4):**
- Register the API app with `identifier_uris = ["api://sentinel-backend"]` + an **app role** (`Incident.Write`, `allowed_member_types = ["Application"]`); grant it to the `sentinel-gha` SP via `azuread_app_role_assignment`.
- Validate inbound JWTs against `https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys` (PyJWT `PyJWKClient`): check signature (RS256), `aud`, `iss`, `exp`, and `roles` contains `Incident.Write`. Tenant + audience are public config.

---

## Architecture Change: DigitalOcean → Azure App Service

**Why:** DigitalOcean requires a payment method even with the $200 student credit. Azure Student account is already active with $139 credit, and App Service F1 tier is always-free (no credit consumed).

**Impact on sentinel-deployment ARCHITECTURE.md:**
- Replace all DigitalOcean references (App Platform, Container Registry, `doctl`) with Azure equivalents
- Deploy target: Azure App Service (F1 free tier)
- Container registry: Azure Container Registry (free 100 GB for 12 months) — OR skip containers entirely and use App Service's native Python deployment (zip deploy)
- GHA actions: `azure/login@v2` + `azure/webapps-deploy@v3` instead of `doctl`
- Secrets: `AZURE_CREDENTIALS` instead of `DIGITALOCEAN_ACCESS_TOKEN`
