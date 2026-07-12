# task-3 — Datadog monitors (deploy-failure + runtime-health)   ·   [category-2-sentinel-deployment / phase-2-deploy-pipeline]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-deployment` |
| **Phase branch** | `impl/deploy-phase-2-deploy-pipeline` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-deployment/ARCHITECTURE.md §6.3, §4 (A/B/C); master §2 |
| **Depends on** | [[task-2-ci-app-deployment]] (produces the events), infra [[task-2-event-grid-module]] + [[task-3-functions-bridge-module]] (webhook target) |
| **Referenced by** | backend [[task-3-ci-incident-response]] (triggered by these) |

## Spec
The two monitors that turn Datadog signal into a `repository_dispatch`. Defined as code
(Datadog Terraform provider OR versioned monitor JSON + apply script — pick one; document).

**Artifacts:**
- `sentinel-deploy-failure` — event monitor on `deploy_status:failed` (Condition C).
- `sentinel-runtime-health` — metric/HTTP monitor: App Service 5xx rate over threshold OR failed synthetic pings to `/` and `/health` (Condition B).
- Both notify the same webhook channel → Event Grid topic (infra 3.2 endpoint+key) → Function → dispatch.
- Monitor hygiene (§6.3): renotify OFF, recovery period, 5-min eval window on runtime monitor.
- **Files:** `datadog/monitors/*.json` (or `*.tf`) + `datadog/README.md` (webhook setup, tag→evidence-class mapping).

## Prerequisites
- [ ] ⛔ B6 Datadog account + API/app keys. [ ] infra Event Grid endpoint+key (task 3.2). [ ] Function bridge (task 3.3).
- [ ] Deploy events already flowing (task 2.2) to test the event monitor.

## Acceptance Criteria
- [ ] Both monitors defined as code with the exact trigger conditions (C = deploy_status:failed event; B = 5xx/failed pings).
- [ ] Webhook wired to the Event Grid topic; tags let agents distinguish B vs C.
- [ ] Hygiene settings applied so demos fire predictably.

## Tests
- **Validate:** JSON/HCL lint; dry-run the monitor definition.
- **Integration (⛔ B6):** trigger a failed deploy → deploy-failure monitor fires → dispatch reaches sentinel repo. Break `GET /` (PR #11 style) → runtime-health fires.
- **Quality gate:** `--repo deployment`.

## How to Verify (phase gate — end of Category 2)
1. Monitor definitions validate.
2. (wired) a Condition-C PR fires `sentinel-deploy-failure`; a Condition-B PR fires `sentinel-runtime-health`; both produce an `incident-alert` dispatch in the sentinel repo Actions tab.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_⛔ B6 (Datadog) + infra Event Grid/Function. Definitions writable now._
