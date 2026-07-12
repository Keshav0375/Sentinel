# task-1 — `dd-report` composite action   ·   [category-2-sentinel-deployment / phase-2-deploy-pipeline]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-deployment` |
| **Phase branch** | `impl/deploy-phase-2-deploy-pipeline` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-deployment/ARCHITECTURE.md §3.2 |
| **Depends on** | — |
| **Referenced by** | [[task-2-ci-app-deployment]] (every reporting stage) |

## Spec
Local composite action so every pipeline stage calls one Datadog reporter instead of
copy-pasted curl blocks.

**Files created:** `.github/actions/dd-report/action.yml`
- Inputs: `title`, `text`, `tags` (comma list), `alert-type` (`info|error`), optional `log-payload`, `dd-api-key`, `dd-site` (default `datadoghq.com`).
- Steps: `send_dd_event` → POST `api.datadoghq.com/api/v1/events`; if `log-payload` set, `send_dd_log` → POST `https://http-intake.logs.${DD_SITE}/api/v2/logs` (§3.2 helper bodies).
- Mask the API key; fail soft (report failures shouldn't break the deploy record).

## Prerequisites
- [ ] actionlint. [ ] ⛔ B6 (Datadog key) only for live verify.

## Acceptance Criteria
- [ ] `action.yml` validates under actionlint; inputs match §3.2; key masked.
- [ ] Event + log paths both implemented; `dd-site` configurable.

## Tests
- **Lint:** actionlint, yamllint.
- **Integration (⛔ B6):** call the action with a real key → event appears in Datadog Events Explorer.
- **Quality gate:** `--repo deployment`.

## How to Verify (phase gate)
1. actionlint clean.
2. (with DD key) a workflow step using the action posts a visible test event/log to Datadog.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live verify ⛔ B6. YAML + lint now._
