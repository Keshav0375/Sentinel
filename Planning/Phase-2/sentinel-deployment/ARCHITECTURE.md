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
# Login to Azure — OIDC via azure/login@v2 (client-id/tenant-id/subscription-id;
# no client secret exists, see §3.4)

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

#### Stage 5: Record Deployment in PostgreSQL (if: always())

Every deploy attempt — success or failure — gets a row in Sentinel's PostgreSQL
`deployments` table. This is the data the Analysis agent's `get_deploy_details` tool
and the deploy ↔ incident correlation depend on. **Failed deploys matter most** —
they're exactly the rows incidents join against.

```bash
# OIDC login already done in Stage 3 (azure/login@v2)
sudo apt-get install -y postgresql-client

DB_PASS=$(az keyvault secret show --vault-name sentinel-kv \
  --name db-password --query value -o tsv)

PGPASSWORD="$DB_PASS" psql \
  "host=sentinel-pg.postgres.database.azure.com dbname=sentinel user=sentinel_admin sslmode=require" <<SQL
INSERT INTO deployments
  (service, pr_number, commit_sha, author, deploy_status, gha_run_id, files_changed, metadata)
VALUES
  ('dummy-api', ${PR_NUMBER}, '${SHORT_SHA}', '${PR_AUTHOR}', '${STATUS}',
   ${GITHUB_RUN_ID}, '${FILES_CHANGED_JSON}'::jsonb,
   jsonb_build_object('failed_stage', '${FAILED_STAGE:-none}', 'version', '${APP_VERSION}'));
SQL
```

Notes:
- Runs `if: always()` — failed builds/deploys are recorded with their `failed_stage`.
- Uses the shared OIDC identity's Key Vault read role for `db-password` — no DB
  credentials stored as GitHub secrets.
- `incident_id` stays NULL here; the backend backfills it when an incident
  correlates to this deploy.
- Implemented via sentinel's shared composite actions, referenced cross-repo:
  `uses: Keshav0375/Sentinel/.github/actions/get-kv-secrets@main` and
  `uses: Keshav0375/Sentinel/.github/actions/psql-exec@main` — one SQL/secret
  implementation maintained in one place.

#### Stage 6: Final Summary (if: always())

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

These helpers live in a **local composite action** (`.github/actions/dd-report/`) so
every stage calls one implementation instead of copy-pasted curl blocks. Inputs:
`title`, `tags`, `alert-type`, optional structured log payload.

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
      - name: Record deployment in PostgreSQL
        if: always()
      - name: Report final summary
        if: always()
```

**Build → Deploy → Verify → Record → Summary.** No image push stage — zip deploy
goes directly to App Service. The record stage writes the `deployments` row that
Sentinel's agents correlate incidents against.

### 3.4 Required GitHub Secrets

| Secret | Description | How Set |
|--------|-------------|---------|
| `AZURE_CLIENT_ID` | OIDC app client ID | Auto-pushed by sentinel-infra Terraform |
| `AZURE_TENANT_ID` | Azure AD tenant ID | Auto-pushed by sentinel-infra Terraform |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID | Auto-pushed by sentinel-infra Terraform |
| `DD_API_KEY` | Datadog API key | Manual |
| `DEPLOYED_APP_URL` | Public URL (e.g. `https://dummy-api.azurewebsites.net`) | Manual |

DB access for the record-deployment stage needs no GitHub secret — the OIDC identity
reads `db-password` from Key Vault at runtime. The demo-PR workflow needs no backend
access at all (scenario templates are static).

**No `AZURE_CLIENT_SECRET`** — uses OIDC workload identity federation.
OIDC federated credentials are provisioned by sentinel-infra Terraform (see sentinel-infra ARCHITECTURE.md §4).
GitHub secrets for AZURE_CLIENT_ID/TENANT_ID/SUBSCRIPTION_ID are auto-pushed by Terraform's `github_actions_secret` resource.

---

## 4. Demo PR Taxonomy — Three Conditions

Demo PRs are created by `ci_demo_prs.yml` (manual `workflow_dispatch` with a scenario
matrix). It is fully self-contained — **no backend involvement**: each scenario in
the matrix carries its scripted file changes plus a pre-written, realistic PR title
and description. The workflow applies the changes on a branch and opens the PR.
(The backend's `/generate/pr-content` agent is used only by `ci_incident_response.yml`
to write **rollback** PR content — see sentinel ARCHITECTURE §3.4.)

Every demo PR falls into one of three conditions:

| Condition | Deploy pipeline | App at runtime | Datadog trigger | Sentinel outcome |
|-----------|----------------|----------------|-----------------|------------------|
| **A — clean** | Green | Healthy | `deploy_status:succeeded` event only (no monitor fires) | No incident. Baseline history for episodic memory. |
| **B — runtime failure** | **Green** (verify passes) | **Breaks after deploy** — site fails under real traffic | Runtime-health monitor fires (5xx rate / failed health pings) | Incident → agents correlate symptoms with the **last successful deploy** (deployments table) → revert PR |
| **C — deploy failure** | **Red** (build/deploy/verify fails) | Old version usually keeps serving (Oryx build failure leaves the previous container running) | Deploy-failure event monitor fires on `deploy_status:failed` | Incident → agents identify the failed deploy from the event + CI context → revert PR |

**The B/C nuance matters for the agents:** in C, production often still serves the
previous version — the revert PR heals **main's deployability**. In B, production is
actually broken — the revert PR heals **the live site**. Both paths end in a revert
PR, but the evidence differs: C leans on the deploy event and pipeline logs; B leans
on runtime error logs plus "what deployed most recently and succeeded?" — which is
exactly the query the `deployments` table answers.

### 4.1 PR Sequence

| # | PR Title | What It Does | Expected Datadog Signal | Condition |
|---|----------|-------------|------------------------|-----------|
| 1 | `feat: initial dummy-api` | Working app + deploy pipeline | `deploy_status:succeeded` | A |
| 2 | `feat: add /info endpoint` | Trivial app change, clean deploy | `deploy_status:succeeded` | A |
| 3 | `fix: break requirements` | Add `nonexistent-package==1.0.0` to requirements.txt | `deploy_status:failed`, `failed_stage:deploy` (pip install fails on server) | C |
| 4 | `fix: repair requirements` | Remove the bad package | `deploy_status:succeeded` | A |
| 5 | `feat: break health check` | Change `/health` to return 503 | `deploy_status:failed`, `failed_stage:verify` | C |
| 6 | `fix: restore health check` | Revert to 200 | `deploy_status:succeeded` | A |
| 7 | `feat: add slow startup` | Add 60s `asyncio.sleep` in lifespan | `deploy_status:failed`, `failed_stage:verify` (health check timeout) | C |
| 8 | `fix: remove slow startup` | Remove the sleep | `deploy_status:succeeded` | A |
| 9 | `feat: wrong version env` | Hardcode `/version` to return `"wrong"` | `deploy_status:failed`, `failed_stage:verify` (version mismatch) | C |
| 10 | `fix: use env var for version` | Restore `APP_VERSION` from env | `deploy_status:succeeded` | A |
| 11 | `feat: add homepage caching` | `GET /` returns 500 on every request; `/health` and `/version` untouched — **verify passes** | `deploy_status:succeeded`, then runtime-health monitor fires on 5xx rate | **B** |
| 12 | `fix: remove broken caching` | Restore `GET /` | `deploy_status:succeeded` | A |
| 13 | `feat: tune health reporting` | `/health` returns 200 for the first ~5 min of uptime, then 503 — **verify window passes** | `deploy_status:succeeded`, then runtime-health monitor fires on failed health pings | **B** |
| 14 | `fix: restore stable health` | Revert the delayed degradation | `deploy_status:succeeded` | A |

**Notes:**
- PR #3 changed from "break Dockerfile" (no Docker anymore) to "break
  requirements.txt" — pip install failure on Azure's Oryx build is the equivalent
  failure mode for zip deploy.
- PRs #11 and #13 exist specifically because the verify stage only checks `/health`
  and `/version` — a break anywhere else (or a delayed break) sails through the
  pipeline and can only be caught by runtime monitoring. This is the condition that
  exercises Sentinel's real diagnostic value.

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
│   ├── actions/
│   │   └── dd-report/             # Local composite action: Datadog event + log reporting
│   └── workflows/
│       ├── ci_app_deployment.yml  # Build → Deploy → Verify → Record → Report
│       └── ci_demo_prs.yml        # workflow_dispatch — scenario PRs from static templates (no backend)
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

### 6.3 Monitors — What Triggers Sentinel

Two Datadog monitors, one per failure condition (§4):

| Monitor | Type | Fires On | Covers |
|---------|------|----------|--------|
| `sentinel-deploy-failure` | Event monitor | Any event tagged `deploy_status:failed` (shipped by this pipeline) | **Condition C** — build/deploy/verify failures |
| `sentinel-runtime-health` | Metric / HTTP monitor | App Service 5xx rate over threshold, or failed pings against `GET /health` + `GET /` (native Azure integration metrics or a Datadog synthetic check) | **Condition B** — runtime failures that passed verify |

Both monitors notify the same webhook channel → Event Grid → Azure Function →
`repository_dispatch` on the sentinel repo. The alert's tags tell the agents which
class fired (`deploy_status:failed` vs a runtime alert), which changes the evidence
they weigh: CI logs + the deploy event for C; runtime error logs + "most recent
successful deploy" correlation (PostgreSQL `deployments` table) for B.

Monitor settings to keep the demo sane: renotify OFF, require a recovery period
before re-alerting, and a short evaluation window (5 min) on the runtime monitor so
condition-B demos fire while the room is still watching.

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
- [ ] OIDC federated credential for this repo — provisioned by sentinel-infra Terraform (see sentinel-infra ARCHITECTURE.md §4); no service principal secret to create
- [ ] Note the app URL: `https://dummy-api.azurewebsites.net`

### GitHub (sentinel-deployment repo)
- [ ] Create repo manually
- [ ] Secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` auto-pushed by Terraform; add manually: `DD_API_KEY`, `DEPLOYED_APP_URL`
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
