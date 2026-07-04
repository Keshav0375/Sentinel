# sentinel-deployment — Architecture Document

> **Purpose:** A near-trivial FastAPI app deployed to Azure App Service (F1 free tier)
> via GitHub Actions. The app itself is a dummy target — the real value is the
> **deployment pipeline**, which ships structured logs and events to Datadog on every
> PR merge. Successful deploys, failed builds, broken health checks — all land as
> real Datadog signal that Sentinel's agents can later analyze.

---

## 1. System Overview

```
PR merged to main
       │
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     ci_app_deployment.yml (GHA workflow)                        │
│                                                                      │
│  ┌───────────┐    ┌───────────┐    ┌───────────┐                    │
│  │   BUILD   │───►│  DEPLOY   │───►│  VERIFY   │                    │
│  │ pip + zip │    │ az webapp │    │ /health   │                    │
│  │ package   │    │ deploy    │    │ /version  │                    │
│  └─────┬─────┘    └─────┬─────┘    └─────┬─────┘                    │
│        │                │                │                           │
│        ▼                ▼                ▼                           │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │             Datadog Events API + Log Intake API              │    │
│  │  Every stage reports: stage, status, version, error detail   │    │
│  └──────────────────────────────────────────────────────────────┘    │
│        │                                                             │
│        ▼                                                             │
│  ┌──────────────────┐                                                │
│  │  FINAL SUMMARY   │  (if: always())                                │
│  │  Datadog Event   │  title: "Deploy {succeeded|failed} PR #N"      │
│  │  + pipeline log  │  tags: version, stage, status                  │
│  └──────────────────┘                                                │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │  Azure App       │
                    │  Service (F1)    │
                    │  dummy-api       │
                    │  GET /health     │
                    │  GET /version    │
                    │  GET /           │
                    └──────────────────┘
```

**Key insight:** The deployment pipeline (GHA) is the thing being observed, not the
app. Every PR merge = one deploy attempt. Some succeed, some fail. Datadog accumulates
a real history of deployment events that Sentinel can later query for incident analysis.

**No Docker.** Azure App Service F1 (free tier) does not support container deploys.
The app is deployed as a zip package using `az webapp deploy`. Azure's Oryx build
system handles `pip install` from `requirements.txt` on the server side.

---

## 2. The App — Intentionally Minimal

The app exists only to be deployed to and verified against. No business logic.

### 2.1 Endpoints

| Method | Path | Response | Purpose |
|--------|------|----------|---------|
| `GET` | `/` | `{"message": "ok", "service": "dummy-api"}` | Basic hello-world |
| `GET` | `/health` | `{"status": "ok", "uptime_seconds": N}` | Deploy verification target |
| `GET` | `/version` | `{"version": "pr-47-a3f9c2", "service": "dummy-api"}` | Confirm which PR/SHA is live |

### 2.2 Startup Behavior

On boot, emit ONE structured log line to stdout:

```json
{
  "timestamp": "2026-07-01T12:00:00.000Z",
  "level": "info",
  "message": "app.startup",
  "app_version": "pr-47-a3f9c2",
  "dd.service": "dummy-api",
  "dd.env": "dev",
  "dd.version": "pr-47-a3f9c2"
}
```

The app doesn't talk to Datadog — the GHA pipeline does.

### 2.3 Tech Stack (app only)

```
fastapi>=0.110.0
uvicorn>=0.29.0
pydantic-settings>=2.0.0
```

Three dependencies.

### 2.4 App Config

```python
class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_version: str = "local-dev"
    dd_service: str = "dummy-api"
    dd_env: str = "dev"
    port: int = 8000
```

### 2.5 Azure App Service Startup

App Service F1 runs Python apps via Oryx. The startup command is configured in the
App Service settings:

```
gunicorn --bind=0.0.0.0 --timeout 600 -k uvicorn.workers.UvicornWorker app.main:app
```

Note: F1 uses shared compute. `gunicorn` with one uvicorn worker is the standard
Azure pattern for Python + async frameworks.

---

## 3. Deployment Pipeline — The Core of the Project

### 3.1 `ci_app_deployment.yml` — Triggered on PR merge to main

**Trigger:** `push` to `main` (fires after squash-merge from any PR)

Every stage reports its outcome to Datadog. Failures are never swallowed —
a failed build is just as visible in Datadog as a successful deploy.

#### Stage 1: Checkout + Metadata

```yaml
- Checkout repo
- Extract from merge commit / GHA context:
    PR_NUMBER   (from commit message or github.event)
    SHORT_SHA   (github.sha[:7])
    PR_TITLE    (from github.event.head_commit.message)
    APP_VERSION = "pr-${PR_NUMBER}-${SHORT_SHA}"
```

#### Stage 2: Build (zip package)

```bash
# Create deployment package
mkdir -p deploy_package
cp -r app/ deploy_package/app/
cp requirements.txt deploy_package/
cd deploy_package && zip -r ../deploy.zip . && cd ..
```

No Docker build — just package the app files + requirements.txt into a zip.
Azure's Oryx build system runs `pip install -r requirements.txt` on the server.

**On failure** (missing files, zip error):

```
POST https://api.datadoghq.com/api/v1/events
{
  "title": "Build FAILED for PR #${PR_NUMBER}: ${PR_TITLE}",
  "text": "${BUILD_ERROR_OUTPUT}",
  "tags": ["version:${APP_VERSION}", "service:dummy-api", "env:dev",
           "stage:build", "deploy_status:failed"],
  "alert_type": "error"
}
```

#### Stage 3: Deploy

```bash
# Login to Azure
az login --service-principal -u $AZURE_CLIENT_ID -p $AZURE_CLIENT_SECRET --tenant $AZURE_TENANT_ID

# Set app version env var
az webapp config appsettings set \
  --resource-group $AZURE_RG \
  --name dummy-api \
  --settings APP_VERSION="${APP_VERSION}"

# Deploy zip package
az webapp deploy \
  --resource-group $AZURE_RG \
  --name dummy-api \
  --src-path deploy.zip \
  --type zip
```

**On failure** (auth error, deploy rejected, app crash):

```
Datadog Event: stage:deploy, deploy_status:failed
```

#### Stage 4: Verify

```bash
# Wait for app to restart after deploy (F1 cold starts can take 30-60s)
sleep 30

# Health check (retry up to 3 times with 10s gap)
for i in 1 2 3; do
  HEALTH=$(curl -sf ${DEPLOYED_APP_URL}/health) && break
  sleep 10
done

# Version check
LIVE_VERSION=$(curl -sf ${DEPLOYED_APP_URL}/version | jq -r '.version')
if [[ "$LIVE_VERSION" != "${APP_VERSION}" ]]; then
  # Report: version mismatch — old version still serving
fi
```

**Important:** F1 tier apps sleep after idle and have cold-start latency.
The verify step retries with a 30s initial wait + 3 attempts.

**On failure** (health check fails, version mismatch):

```
Datadog Event: stage:verify, deploy_status:failed
```

#### Stage 5: Final Summary (if: always())

Runs regardless of which stage succeeded or failed.

```json
{
  "title": "Deployment ${STATUS} for PR #${PR_NUMBER}: ${PR_TITLE}",
  "tags": ["version:${APP_VERSION}", "service:dummy-api", "env:dev",
           "deploy_status:${STATUS}", "failed_stage:${FAILED_STAGE:-none}"],
  "alert_type": "info or error"
}
```

Also ships a structured log line via the Datadog Log Intake API:

```json
{
  "message": "deploy.completed",
  "ddsource": "github-actions",
  "ddtags": "version:pr-47-a3f9c2,service:dummy-api,env:dev",
  "hostname": "gha-runner",
  "service": "dummy-api",
  "deploy": {
    "pr_number": 47,
    "version": "pr-47-a3f9c2",
    "pr_title": "feat: add retry config",
    "status": "succeeded",
    "failed_stage": "none",
    "duration_seconds": 95,
    "stages": {
      "build": "succeeded",
      "deploy": "succeeded",
      "verify": "succeeded"
    }
  }
}
```

### 3.2 Datadog Reporting Helper

```bash
send_dd_event() {
  local title="$1" text="$2" alert_type="$3" tags="$4"
  curl -sf -X POST "https://api.datadoghq.com/api/v1/events" \
    -H "DD-API-KEY: ${DD_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"title\":\"${title}\",\"text\":\"${text}\",\"tags\":[${tags}],\"alert_type\":\"${alert_type}\",\"source_type_name\":\"github\"}"
}

send_dd_log() {
  local payload="$1"
  curl -sf -X POST "https://http-intake.logs.${DD_SITE}/api/v2/logs" \
    -H "DD-API-KEY: ${DD_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "[${payload}]"
}
```

### 3.3 GHA Workflow Structure

```yaml
name: "[deployment] deploy — build and ship"

on:
  push:
    branches: [main]

env:
  DD_SITE: datadoghq.com
  DD_SERVICE: dummy-api
  DD_ENV: dev

jobs:
  build-deploy-verify:
    name: Build Deploy and Verify
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
      - name: Extract PR metadata
      - name: Build zip package
      - name: Report build failure
        if: failure()
      - name: Login to Azure
      - name: Deploy to App Service
      - name: Report deploy failure
        if: failure()
      - name: Verify deployment
      - name: Report verify failure
        if: failure()
      - name: Report final summary
        if: always()
```

**Three stages, not four.** No push step needed — zip deploy goes directly to
App Service. The pipeline is simpler than the original Docker-based design.

### 3.4 Required GitHub Secrets

| Secret | Description | How Set |
|--------|-------------|---------|
| `AZURE_CLIENT_ID` | OIDC app client ID | Auto-pushed by sentinel-infra Terraform |
| `AZURE_TENANT_ID` | Azure AD tenant ID | Auto-pushed by sentinel-infra Terraform |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID | Auto-pushed by sentinel-infra Terraform |
| `DD_API_KEY` | Datadog API key | Manual |
| `DEPLOYED_APP_URL` | Public URL (e.g. `https://dummy-api.azurewebsites.net`) | Manual |

**No `AZURE_CLIENT_SECRET`** — uses OIDC workload identity federation.
OIDC federated credentials are provisioned by sentinel-infra Terraform (see sentinel-infra ARCHITECTURE.md §4).
GitHub secrets for AZURE_CLIENT_ID/TENANT_ID/SUBSCRIPTION_ID are auto-pushed by Terraform's `github_actions_secret` resource.

---

## 4. Demo PR Sequence

Each PR creates a real deploy attempt. Some succeed, some intentionally fail.

| # | PR Title | What It Does | Expected Datadog Signal |
|---|----------|-------------|------------------------|
| 1 | `feat: initial dummy-api` | Working app + deploy pipeline | `deploy_status:succeeded` |
| 2 | `feat: add /info endpoint` | Trivial app change, clean deploy | `deploy_status:succeeded` |
| 3 | `fix: break requirements` | Add `nonexistent-package==1.0.0` to requirements.txt | `deploy_status:failed`, `failed_stage:deploy` (pip install fails on server) |
| 4 | `fix: repair requirements` | Remove the bad package | `deploy_status:succeeded` |
| 5 | `feat: break health check` | Change `/health` to return 503 | `deploy_status:failed`, `failed_stage:verify` |
| 6 | `fix: restore health check` | Revert to 200 | `deploy_status:succeeded` |
| 7 | `feat: add slow startup` | Add 60s `asyncio.sleep` in lifespan | `deploy_status:failed`, `failed_stage:verify` (health check timeout) |
| 8 | `fix: remove slow startup` | Remove the sleep | `deploy_status:succeeded` |
| 9 | `feat: wrong version env` | Hardcode `/version` to return `"wrong"` | `deploy_status:failed`, `failed_stage:verify` (version mismatch) |
| 10 | `fix: use env var for version` | Restore `APP_VERSION` from env | `deploy_status:succeeded` |

**Note:** Demo PR #3 changed from "break Dockerfile" (no Docker anymore) to
"break requirements.txt" — this triggers a pip install failure on Azure's
Oryx build, which is the equivalent failure mode for zip deploy.

---

## 5. Repository Structure

```
sentinel-deployment/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app: 3 routes + startup log
│   └── config.py            # AppConfig (pydantic-settings)
├── tests/
│   └── test_app.py          # Endpoint tests (health, version, root)
├── .github/
│   └── workflows/
│       └── ci_app_deployment.yml  # Build → Deploy → Verify → Report
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

No Dockerfile, no app.yaml, no container registry. Zip deploy keeps it simple.

---

## 6. Datadog Schema

### 6.1 Events (timeline markers)

Every deploy produces at least one Datadog Event. Failed deploys produce two
(stage failure + final summary).

**Event tags (always present):**

| Tag | Value | Example |
|-----|-------|---------|
| `version` | PR number + SHA | `version:pr-47-a3f9c2` |
| `service` | Fixed | `service:dummy-api` |
| `env` | Fixed | `env:dev` |
| `deploy_status` | `succeeded` or `failed` | `deploy_status:failed` |
| `failed_stage` | Which stage failed | `failed_stage:build` / `failed_stage:none` |

### 6.2 Logs (searchable records)

Each deploy ships one structured log line via the HTTP Log Intake API.

Queryable in Datadog Log Explorer:
- `deploy_status:failed` — all failed deploys
- `failed_stage:build` — all build failures
- `@deploy.pr_number:47` — everything about PR #47's deploy
- `version:pr-47-*` — filter by deploy version

---

## 7. What This Enables for Sentinel

After 10+ PRs, Datadog contains:

1. **Deploy events on a timeline** — visible in Events Explorer, tagged with PR + version
2. **Deploy logs** — searchable by status, stage, PR number
3. **Failure patterns** — build failures (bad deps), health check failures, version mismatches
4. **Before/after correlation** — events mark exactly when each deploy happened

When the full Sentinel pipeline is connected:
- Datadog monitor triggers on `deploy_status:failed` → webhook → Event Grid → Sentinel GHA
- Sentinel agents fetch recent deploy logs via Datadog API
- Agents correlate the failed deploy with the PR that caused it
- Sentinel drafts a revert PR on sentinel-deployment

---

## 8. Prerequisites & Setup Checklist

### Datadog
- [ ] Activate Student Pack Datadog offer (Pro, 10 servers, 2 years)
- [ ] Note Datadog site (US1: `datadoghq.com` or US5: `us5.datadoghq.com`)
- [ ] Generate DD_API_KEY from Organization Settings → API Keys
- [ ] Test Events API: `curl -X POST "https://api.datadoghq.com/api/v1/events" -H "DD-API-KEY: <key>" -d '{"title":"test","text":"hello"}'`

### Azure
- [ ] Create resource group: `az group create --name sentinel-rg --location eastus`
- [ ] Create App Service plan (F1): `az appservice plan create --name sentinel-plan --resource-group sentinel-rg --sku F1 --is-linux`
- [ ] Create web app: `az webapp create --resource-group sentinel-rg --plan sentinel-plan --name dummy-api --runtime "PYTHON:3.12"`
- [ ] Configure startup command: `az webapp config set --resource-group sentinel-rg --name dummy-api --startup-file "gunicorn --bind=0.0.0.0 --timeout 600 -k uvicorn.workers.UvicornWorker app.main:app"`
- [ ] Create service principal: `az ad sp create-for-rbac --name sentinel-deploy-sp --role contributor --scopes /subscriptions/<sub-id>/resourceGroups/sentinel-rg`
- [ ] Note the app URL: `https://dummy-api.azurewebsites.net`

### GitHub (sentinel-deployment repo)
- [ ] Create repo manually
- [ ] Add secrets: `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`, `AZURE_RG`, `DD_API_KEY`, `DEPLOYED_APP_URL`
- [ ] Branch protection on main: require PR, require `Deploy` workflow to pass

### Local Development
- [ ] Python 3.12 available
- [ ] `pip install -r requirements.txt && uvicorn app.main:app --reload` works locally

---

## 9. Cost Breakdown

| Resource | Monthly Cost | Covered By |
|----------|-------------|------------|
| Azure App Service (F1) | Free | Always-free tier |
| Datadog Pro (events + logs) | Free | Student Pack (2 years) |
| GHA minutes (deploy runs) | Free | GitHub Pro (3,000 min/month) |
| **Total** | **$0/month** | **No credits consumed** |

GHA usage estimate:
- `ci_app_deployment.yml`: ~2 min per run (zip build + deploy + verify)
- ~20 merges/month = 40 min
- Well within 3,000 min/month quota
