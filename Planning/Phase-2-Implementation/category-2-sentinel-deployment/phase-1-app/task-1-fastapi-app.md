# task-1 — FastAPI app (3 routes + startup log + config)   ·   [category-2-sentinel-deployment / phase-1-app]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-deployment` |
| **Local path** | `Agentic-Engineering/Sentinel-development-project/Sentinel-deployment` |
| **Phase branch** | `dev/deploy-phase-1-app` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-deployment/ARCHITECTURE.md §2 (all), §5 |
| **Depends on** | — (independent of infra to build; deploy target is infra 3.4) |
| **Referenced by** | [[task-2-app-tests]], [[task-2-ci-app-deployment]], [[task-1-demo-prs-workflow]] |

## Spec
Intentionally minimal app — the deploy pipeline is the product; this is the target.

**Files created:**
- `app/__init__.py`
- `app/main.py` — FastAPI app; `GET /` → `{"message":"ok","service":"dummy-api"}`; `GET /health` → `{"status":"ok","uptime_seconds":N}`; `GET /version` → `{"version": settings.app_version, "service":"dummy-api"}`; startup lifespan emits ONE structured JSON log line `app.startup` with dd.service/env/version (§2.2).
- `app/config.py` — `AppConfig(BaseSettings)` per §2.4: `app_version`, `dd_service`, `dd_env`, `port`.
- `requirements.txt` — `fastapi>=0.110`, `uvicorn>=0.29`, `pydantic-settings>=2.0` (§2.3).
- `.env.example` — from [../env-examples/deployment.env.example](../../env-examples/deployment.env.example) app section.
- `.gitignore` (extend), `README.md` (run locally: `uvicorn app.main:app --reload`).

## Prerequisites
- [ ] Python 3.12. [ ] pip.

## Acceptance Criteria
- [ ] `uvicorn app.main:app` boots; all three routes return the documented shapes.
- [ ] Startup emits exactly one `app.startup` structured line with dd tags + app_version from env.
- [ ] `/version` reflects `APP_VERSION` env (so demo PRs that change version are observable).

## Tests
- **Unit:** covered by [[task-2-app-tests]] (kept as its own task so the app PR stays focused; both land in this phase).
- **Quality gate:** `python scripts/quality_gate.py --repo deployment --path <repo>` (ruff · pytest).

## How to Verify (phase gate)
1. `pip install -r requirements.txt && uvicorn app.main:app --port 8000`.
2. `curl :8000/ :8000/health :8000/version` → documented JSON; logs show one `app.startup` line.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none — fully local._
