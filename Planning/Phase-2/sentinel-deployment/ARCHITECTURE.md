# sentinel-deployment — Architecture Document

> **Purpose:** A near-trivial FastAPI app deployed to DigitalOcean App Platform via
> GitHub Actions. The app itself is a dummy target — the real value is the **deployment
> pipeline**, which ships structured logs and events to Datadog on every PR merge.
> Successful deploys, failed builds, broken health checks, version mismatches — all
> land as real Datadog signal that Sentinel's agents can later analyze.

---

## 1. System Overview

```
PR merged to main
       │
       ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     deploy.yml (GHA workflow)                       │
│                                                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐      │
│  │  BUILD   │───►│  PUSH    │───►│  DEPLOY  │───►│  VERIFY  │      │
│  │ docker   │    │ to DOCR  │    │ DO App   │    │ /health  │      │
│  │ build    │    │          │    │ Platform │    │ /version │      │
│  └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘      │
│       │               │               │               │            │
│       ▼               ▼               ▼               ▼            │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │              Datadog Events API + Log Intake API            │    │
│  │  Every stage reports: stage, status, version, error detail  │    │
│  └─────────────────────────────────────────────────────────────┘    │
│       │                                                            │
│       ▼                                                            │
│  ┌──────────────────┐                                              │
│  │  FINAL SUMMARY   │  (if: always())                              │
│  │  Datadog Event   │  title: "Deploy {succeeded|failed} PR #N"    │
│  │  + pipeline log  │  tags: version, stage, status                │
│  └──────────────────┘                                              │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │  DO App Platform  │
                    │  (dummy-api)      │
                    │  GET /health      │
                    │  GET /version     │
                    │  GET /            │
                    └──────────────────┘
```

**Key insight:** The deployment pipeline (GHA) is the thing being observed, not the
app. Every PR merge = one deploy attempt. Some succeed, some fail. Datadog accumulates
a real history of deployment events that Sentinel can later query for incident analysis.

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

That's it. No background workers, no queues, no log shipping from the app itself.
The app doesn't talk to Datadog — the GHA pipeline does.

### 2.3 Tech Stack (app only)

```
fastapi>=0.110.0
uvicorn>=0.29.0
pydantic-settings>=2.0.0
```

Three dependencies. Image size: ~80MB.

### 2.4 App Config

```python
class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_version: str = "local-dev"
    dd_service: str = "dummy-api"
    dd_env: str = "dev"
    port: int = 8000
```

---

## 3. Deployment Pipeline — The Core of the Project

### 3.1 `deploy.yml` — Triggered on PR merge to main

**Trigger:** `push` to `main` (fires after squash-merge from any PR)

Every stage reports its outcome to Datadog. Failures are never swallowed —
a failed build is just as visible in Datadog as a successful deploy.

#### Stage 1: Checkout + Metadata

```yaml
- Checkout repo
- Extract from merge commit / GHA context:
    PR_NUMBER   (from commit message or github.event)
    SHORT_SHA   (github.sha[:7])
    PR_TITLE    (from github.event.head_commit.message or API)
    PR_BODY     (from github.event API call)
    APP_VERSION = "pr-${PR_NUMBER}-${SHORT_SHA}"
```

#### Stage 2: Build

```
docker build -t registry.digitalocean.com/{REGISTRY}/dummy-api:${APP_VERSION} .
docker tag ... :latest
```

**On success:** Continue to push.

**On failure** (bad Dockerfile, broken dependency, syntax error):

```
POST https://api.datadoghq.com/api/v1/events
{
  "title": "Build FAILED for PR #${PR_NUMBER}: ${PR_TITLE}",
  "text": "${BUILD_ERROR_OUTPUT}",
  "tags": [
    "version:${APP_VERSION}",
    "service:dummy-api",
    "env:dev",
    "stage:build",
    "deploy_status:failed"
  ],
  "alert_type": "error"
}
```

Workflow exits after reporting. No push, no deploy.

#### Stage 3: Push

```
doctl registry login
docker push registry.digitalocean.com/{REGISTRY}/dummy-api:${APP_VERSION}
docker push registry.digitalocean.com/{REGISTRY}/dummy-api:latest
```

**On failure** (auth issue, registry full, network error):

```
Datadog Event: stage:push, deploy_status:failed
```

#### Stage 4: Deploy

```
# Update the APP_VERSION env var on the DO app
doctl apps update ${DO_APP_ID} --spec <updated-app-spec-with-new-version>

# Trigger deployment
DEPLOYMENT_ID=$(doctl apps create-deployment ${DO_APP_ID} --format ID --no-header)

# Poll until ACTIVE or ERROR (timeout 5 minutes, poll every 15s)
while true; do
  STATUS=$(doctl apps get-deployment ${DO_APP_ID} ${DEPLOYMENT_ID} --format Phase --no-header)
  if [[ "$STATUS" == "ACTIVE" ]]; then break; fi
  if [[ "$STATUS" == "ERROR" || "$STATUS" == "FAILED" ]]; then
    # Report failure and exit
    break
  fi
  sleep 15
done
```

**On failure** (deploy error, timeout, crash loop):

```
Datadog Event: stage:deploy, deploy_status:failed, error detail from DO API
```

#### Stage 5: Verify

```
# Wait 10s for app to stabilize after ACTIVE status
sleep 10

# Health check
HEALTH=$(curl -sf ${DEPLOYED_APP_URL}/health)
if [[ $? -ne 0 ]]; then
  # Report: health check failed
fi

# Version check
LIVE_VERSION=$(curl -sf ${DEPLOYED_APP_URL}/version | jq -r '.version')
if [[ "$LIVE_VERSION" != "${APP_VERSION}" ]]; then
  # Report: version mismatch — old version still serving
fi
```

**On failure** (health check fails, version mismatch):

```
Datadog Event: stage:verify, deploy_status:failed
```

#### Stage 6: Final Summary (if: always())

Runs regardless of which stage succeeded or failed.

```
POST https://api.datadoghq.com/api/v1/events
{
  "title": "Deployment ${STATUS} for PR #${PR_NUMBER}: ${PR_TITLE}",
  "text": "## Deploy Summary\n\n
    **PR:** #${PR_NUMBER}\n
    **Version:** ${APP_VERSION}\n
    **Failed Stage:** ${FAILED_STAGE:-none}\n
    **Error:** ${ERROR_DETAIL:-none}\n
    **GHA Run:** ${RUN_URL}\n\n
    ### PR Description\n
    ${PR_BODY}",
  "tags": [
    "version:${APP_VERSION}",
    "service:dummy-api",
    "env:dev",
    "deploy_status:${STATUS}",
    "failed_stage:${FAILED_STAGE:-none}"
  ],
  "alert_type": "${STATUS == 'succeeded' ? 'info' : 'error'}"
}
```

Additionally, ship a structured **log line** via the Datadog Log Intake API
(POST to `https://http-intake.logs.{DD_SITE}/api/v2/logs`) with the same
metadata — this creates a searchable log entry alongside the event:

```json
{
  "message": "deploy.completed",
  "ddsource": "github-actions",
  "ddtags": "version:pr-47-a3f9c2,service:dummy-api,env:dev",
  "hostname": "gha-runner",
  "service": "dummy-api",
  "status": "info",
  "deploy": {
    "pr_number": 47,
    "version": "pr-47-a3f9c2",
    "pr_title": "feat: add retry config",
    "status": "succeeded",
    "failed_stage": "none",
    "duration_seconds": 142,
    "stages": {
      "build": "succeeded",
      "push": "succeeded",
      "deploy": "succeeded",
      "verify": "succeeded"
    }
  }
}
```

### 3.2 Datadog Reporting Helper

Extract the curl-to-Datadog logic into a reusable shell function used by every stage:

```bash
send_dd_event() {
  local title="$1"
  local text="$2"
  local alert_type="$3"   # info | error | warning
  local tags="$4"          # comma-separated

  curl -sf -X POST "https://api.datadoghq.com/api/v1/events" \
    -H "DD-API-KEY: ${DD_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{
      \"title\": \"${title}\",
      \"text\": \"${text}\",
      \"tags\": [${tags}],
      \"alert_type\": \"${alert_type}\",
      \"source_type_name\": \"github\"
    }"
}

send_dd_log() {
  local payload="$1"

  curl -sf -X POST "https://http-intake.logs.${DD_SITE}/api/v2/logs" \
    -H "DD-API-KEY: ${DD_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "[${payload}]"
}
```

### 3.3 GHA Workflow Structure (YAML outline)

```yaml
name: Deploy

on:
  push:
    branches: [main]

env:
  DD_SITE: datadoghq.com
  DD_SERVICE: dummy-api
  DD_ENV: dev

jobs:
  deploy:
    name: Build → Push → Deploy → Verify
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
      - name: Extract PR metadata
      - name: Build Docker image
      - name: Report build failure
        if: failure()
      - name: Push to DO Container Registry
      - name: Report push failure
        if: failure()
      - name: Deploy to App Platform
      - name: Report deploy failure
        if: failure()
      - name: Verify deployment
      - name: Report verify failure
        if: failure()
      - name: Report final summary
        if: always()
```

**Important:** Use `continue-on-error: false` on each stage step (the default).
The `if: failure()` reporting steps fire when any previous step failed.
The final summary step uses `if: always()` to capture the full picture.

### 3.4 Required GitHub Secrets

| Secret | Description |
|--------|-------------|
| `DIGITALOCEAN_ACCESS_TOKEN` | DO API token for doctl |
| `DO_REGISTRY_NAME` | Container registry name (e.g. `sentinel-registry`) |
| `DO_APP_ID` | App Platform app UUID (set after first manual deploy) |
| `DD_API_KEY` | Datadog API key |
| `DEPLOYED_APP_URL` | Public URL of the deployed app (e.g. `https://dummy-api-xxxxx.ondigitalocean.app`) |

---

## 4. Demo PR Sequence

Each PR creates a real deploy attempt. Some succeed, some intentionally fail.
This builds up a real deployment history in Datadog over time.

| # | PR Title | What It Does | Expected Datadog Signal |
|---|----------|-------------|------------------------|
| 1 | `feat: initial dummy-api` | Working app + deploy pipeline | `deploy_status:succeeded` event + log |
| 2 | `feat: add /info endpoint` | Trivial app change, clean deploy | Another `deploy_status:succeeded` |
| 3 | `fix: break the Dockerfile` | Introduce a typo in Dockerfile (`RUN pip install fasttapi`) | `deploy_status:failed`, `failed_stage:build` |
| 4 | `fix: repair Dockerfile` | Fix the typo | `deploy_status:succeeded` — recovery visible |
| 5 | `feat: break health check` | Change `/health` to return 503 | `deploy_status:failed`, `failed_stage:verify` |
| 6 | `fix: restore health check` | Revert to 200 | `deploy_status:succeeded` |
| 7 | `feat: add slow startup` | Add 30s `asyncio.sleep` in lifespan (simulates timeout) | `deploy_status:failed`, `failed_stage:deploy` (DO health check timeout) |
| 8 | `fix: remove slow startup` | Remove the sleep | `deploy_status:succeeded` |
| 9 | `feat: wrong version env` | Hardcode `/version` to return `"wrong"` | `deploy_status:failed`, `failed_stage:verify` (version mismatch) |
| 10 | `fix: use env var for version` | Restore `APP_VERSION` from env | `deploy_status:succeeded` |

After 10 PRs, Datadog has:
- ~5 successful deploys with full stage timing
- ~5 failed deploys across different failure modes (build, deploy, verify)
- Each event tagged with `version:pr-N-sha`, `failed_stage:X`
- Searchable, filterable, correlatable — real data for Sentinel agents

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
│       └── deploy.yml       # Build → Push → Deploy → Verify → Report
├── Dockerfile
├── app.yaml                 # DO App Platform spec
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

No traffic generator — not needed. The deploy pipeline itself generates all the
Datadog signal we care about.

---

## 6. Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## 7. DO App Spec (`app.yaml`)

```yaml
name: dummy-api
services:
  - name: api
    dockerfile_path: Dockerfile
    source_dir: /
    http_port: 8000
    instance_count: 1
    instance_size_slug: basic-xxs    # ~$5/month
    health_check:
      http_path: /health
    envs:
      - key: APP_VERSION
        value: "local-dev"
      - key: DD_SERVICE
        value: "dummy-api"
      - key: DD_ENV
        value: "dev"
```

---

## 8. Dependencies (`requirements.txt`)

```
fastapi>=0.110.0
uvicorn>=0.29.0
pydantic-settings>=2.0.0
```

Three deps. No structlog, no httpx, no logging handler — the app doesn't ship logs
to Datadog. The GHA pipeline does that via `curl`.

---

## 9. Datadog Schema

### 9.1 Events (timeline markers)

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

### 9.2 Logs (searchable records)

Each deploy also ships one structured log line via the HTTP Log Intake API.

**Log fields:**

```json
{
  "message": "deploy.completed",
  "ddsource": "github-actions",
  "service": "dummy-api",
  "hostname": "gha-runner",
  "ddtags": "version:pr-47-a3f9c2,service:dummy-api,env:dev,deploy_status:succeeded,failed_stage:none",
  "deploy.pr_number": 47,
  "deploy.version": "pr-47-a3f9c2",
  "deploy.pr_title": "feat: add /info endpoint",
  "deploy.status": "succeeded",
  "deploy.failed_stage": "none",
  "deploy.duration_seconds": 142,
  "deploy.stages.build": "succeeded",
  "deploy.stages.push": "succeeded",
  "deploy.stages.deploy": "succeeded",
  "deploy.stages.verify": "succeeded"
}
```

This lets you query in Datadog Log Explorer:
- `deploy_status:failed` — all failed deploys
- `failed_stage:build` — all build failures
- `@deploy.pr_number:47` — everything about PR #47's deploy
- `version:pr-47-*` — filter by deploy version

---

## 10. What This Enables for Sentinel

After 10+ PRs, Datadog contains:

1. **Deploy events on a timeline** — visible in Datadog Events Explorer, each tagged with PR number + version
2. **Deploy logs** — searchable by status, stage, PR number, version
3. **Failure patterns** — build failures, health check failures, version mismatches, deploy timeouts
4. **Before/after correlation** — events mark exactly when each deploy happened, so Sentinel agents can correlate "failure started after PR #5 deployed"

When the full Sentinel pipeline is connected:
- Datadog monitor triggers on `deploy_status:failed` → webhook → Event Grid → Sentinel GHA
- Sentinel agents fetch recent deploy logs via Datadog API
- Agents correlate the failed deploy with the PR that caused it
- Sentinel opens a revert PR on sentinel-deployment

---

## 11. Prerequisites & Setup Checklist

### Datadog
- [ ] Activate Student Pack Datadog offer (Pro, 10 servers, 2 years)
- [ ] Note Datadog site (US1: `datadoghq.com` or US5: `us5.datadoghq.com`)
- [ ] Generate DD_API_KEY from Organization Settings → API Keys
- [ ] Test Events API: `curl -X POST "https://api.datadoghq.com/api/v1/events" -H "DD-API-KEY: <key>" -H "Content-Type: application/json" -d '{"title":"test","text":"hello"}'`

### DigitalOcean
- [ ] Activate $200 student credit
- [ ] Install doctl CLI: `winget install DigitalOcean.Doctl`
- [ ] `doctl auth init` with API token
- [ ] Create Container Registry: `doctl registry create sentinel-registry --subscription-tier starter`
- [ ] First deploy is manual (to get DO_APP_ID): `doctl apps create --spec app.yaml`
- [ ] Note the app URL and app ID for GHA secrets

### GitHub (sentinel-deployment repo)
- [ ] Create repo manually
- [ ] Add secrets: `DIGITALOCEAN_ACCESS_TOKEN`, `DO_REGISTRY_NAME`, `DO_APP_ID`, `DD_API_KEY`, `DEPLOYED_APP_URL`
- [ ] Branch protection on main: require PR, require `Deploy` workflow to pass

### Local Development
- [ ] Docker Desktop installed (for local `docker build` testing)
- [ ] Python 3.12 available
- [ ] `uvicorn app.main:app --reload` works locally

---

## 12. Cost Breakdown

| Resource | Monthly Cost | Covered By |
|----------|-------------|------------|
| DO App Platform (Basic XXS) | ~$5 | $200 DO credit (40 months) |
| DO Container Registry (Starter) | Free | — |
| Datadog Pro (events + logs) | Free | Student Pack (2 years) |
| GHA minutes (deploy runs) | Free | GitHub Pro (3,000 min/month) |
| **Total** | **~$5/month** | **Fully covered by credits** |

GHA usage estimate:
- `deploy.yml`: ~3 min per run (build + push + deploy polling + verify)
- ~20 merges/month = 60 min
- Well within 3,000 min/month quota
