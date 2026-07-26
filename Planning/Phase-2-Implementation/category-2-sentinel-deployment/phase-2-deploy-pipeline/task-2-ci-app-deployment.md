# task-2 — `ci_app_deployment.yml` (Build→Deploy→Verify→Record→Summary)   ·   [category-2-sentinel-deployment / phase-2-deploy-pipeline]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-deployment` |
| **Phase branch** | `dev/deploy-phase-2-deploy-pipeline` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-deployment/ARCHITECTURE.md §3.1, §3.3, §3.4; §6.1/6.2 |
| **Depends on** | [[task-1-fastapi-app]], [[task-1-dd-report-action]]; infra [[task-4-app-service-module]], [[task-2-postgresql-module]], [[task-3-keyvault-module]]; backend [[task-3-composite-actions]] (get-kv-secrets, psql-exec) |
| **Referenced by** | [[task-3-datadog-monitors]], backend `deployments` table consumers |

## Spec
The core pipeline. Fires on push to main; every stage reports to Datadog; the record stage
writes the `deployments` row that backend correlation depends on.

**Files created:** `.github/workflows/ci_app_deployment.yml` — name `[deployment] deploy — build and ship`; `push: [main]`; env DD_SITE/DD_SERVICE/DD_ENV.
- **Stage 1** metadata: PR_NUMBER, SHORT_SHA, PR_TITLE, `APP_VERSION=pr-<n>-<sha>`.
- **Stage 2** build zip (app/ + requirements.txt); on failure → `dd-report` `stage:build deploy_status:failed`.
- **Stage 3** deploy: `azure/login@v2` (OIDC), `az webapp config appsettings set APP_VERSION`, `az webapp deploy --type zip`; on failure → dd-report `stage:deploy`.
- **Stage 4** verify: sleep 30, retry `/health` ×3, `/version` == APP_VERSION; on failure → dd-report `stage:verify`.
- **Stage 5** record (`if: always()`): reuse sentinel's cross-repo actions **`get-db-token`** (short-lived Entra token — **not** `get-kv-secrets`/`db-password`; Postgres is Entra-only) + `psql-exec` INSERT into `deployments` (service, pr_number, commit_sha, author, deploy_status, gha_run_id, files_changed jsonb, metadata{failed_stage,version}). `incident_id` NULL.
- **Stage 6** summary (`if: always()`): dd-report final event + structured `deploy.completed` log (§3.1 payload).
- Secrets (§3.4): AZURE_* (from infra), DD_API_KEY, DEPLOYED_APP_URL, AZURE_RG.

## Prerequisites
- [ ] actionlint. [ ] Cross-repo actions exist (backend 7.3) — else record stage BLOCKED. [ ] ⛔ B1 (App Service, DB, KV), ⛔ B6 (Datadog).

## Acceptance Criteria
- [ ] Workflow validates; Build→Deploy→Verify→Record→Summary present; failure paths report per §3.1.
- [ ] Record stage runs `if: always()` (failed deploys recorded) and writes all `deployments` columns.
- [ ] Uses OIDC (no client secret) + cross-repo `psql-exec`/`get-kv-secrets`.

## Tests
- **Lint:** actionlint, yamllint.
- **Integration (⛔ B1/B6 + backend 7.3):** merge a PR → App Service updated, `/version` matches, a `deployments` row exists, Datadog shows the event.
- **Quality gate:** `--repo deployment`.

## How to Verify (phase gate)
1. actionlint clean.
2. (wired) merge a trivial PR → app redeploys, Datadog event visible, `psql -c 'select * from deployments order by deployed_at desc limit 1'` shows the row.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Record stage needs backend 7.3 (psql-exec/get-kv-secrets). Live run ⛔ B1 + B6. YAML now._
