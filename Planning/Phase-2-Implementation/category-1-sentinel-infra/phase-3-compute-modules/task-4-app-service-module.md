# task-4 — App Service module (F1 target)   ·   [category-1-sentinel-infra / phase-3-compute-modules]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Phase branch** | `impl/infra-phase-3-compute-modules` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §3.6; sentinel-deployment §2.5, §8 |
| **Depends on** | [[task-1-repo-skeleton-and-providers]] |
| **Referenced by** | [[task-1-fastapi-app]], [[task-2-ci-app-deployment]] (deploy target) |

## Spec
Free F1 Linux web app that hosts the dummy-api (deployment repo's zip-deploy target).

**Files created:** `modules/app-service/{main.tf,variables.tf,outputs.tf}`
- `azurerm_service_plan "deployment"` — name `sentinel-deploy-plan`, Linux, sku `F1`.
- `azurerm_linux_web_app "dummy_api"` — name `dummy-api`, python 3.12, app_settings: `APP_VERSION=initial`, `DD_SERVICE=dummy-api`, `DD_ENV=dev`, `SCM_DO_BUILD_DURING_DEPLOYMENT=true`; startup command (gunicorn+uvicorn worker, §2.5) via `site_config.app_command_line`.
- `variables.tf` — `resource_group_name`, `location`.
- `outputs.tf` — `app_url` (`https://dummy-api.azurewebsites.net`), `app_name`.

## Prerequisites
- [ ] terraform CLI. [ ] `dummy-api` name available (⛔ B1 to apply). [ ] F1 available in region (R3).

## Acceptance Criteria
- [ ] Validates; F1 plan + web app; startup command set for gunicorn/uvicorn; DD_* app settings present.
- [ ] Output `app_url` matches what deployment's `DEPLOYED_APP_URL` will use.

## Tests
- **Validate:** validate, tflint, tfsec.
- **Integration (⛔ B1):** apply; `curl https://dummy-api.azurewebsites.net` (404/placeholder pre-deploy is fine — app ships from deployment repo).
- **Quality gate:** `--repo infra`.

## How to Verify (phase gate)
1. `terraform plan -target=module.app-service` → plan + web app.
2. (post-apply) the app URL resolves (serves default page until deployment repo ships the app).

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Apply/verify ⛔ B1. Code writable now._
