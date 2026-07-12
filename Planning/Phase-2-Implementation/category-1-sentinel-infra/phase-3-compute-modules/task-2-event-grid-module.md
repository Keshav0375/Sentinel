# task-2 — Event Grid module   ·   [category-1-sentinel-infra / phase-3-compute-modules]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Phase branch** | `impl/infra-phase-3-compute-modules` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §3.4 |
| **Depends on** | [[task-1-repo-skeleton-and-providers]] |
| **Referenced by** | [[task-3-functions-bridge-module]] (subscription → function), [[task-3-datadog-monitors]] |

## Spec
Event Grid topic that routes Datadog webhooks to the bridge Function.

**Files created:** `modules/event-grid/{main.tf,variables.tf,outputs.tf}`
- `azurerm_eventgrid_topic "sentinel"` — name `sentinel-events`.
- `azurerm_eventgrid_event_subscription "to_function"` — `azure_function_endpoint { function_id = "${var.function_app_id}/functions/bridge" }`.
- `variables.tf` — `resource_group_name`, `location`, `function_app_id`.
- `outputs.tf` — `topic_id`, `topic_endpoint`, `topic_key` (sensitive — Datadog webhook uses it).

**Note:** the subscription depends on the Function existing (task 3.3) → wire ordering in root `main.tf` (task 4.4).

## Prerequisites
- [ ] task 3.3 function app id (or apply in dependency order). [ ] ⛔ B1 to apply.

## Acceptance Criteria
- [ ] Validates; topic + subscription defined; outputs expose endpoint + key for the Datadog monitor webhook.
- [ ] Subscription targets the `bridge` function.

## Tests
- **Validate:** validate, tflint.
- **Integration (⛔ B1, needs 3.3):** apply; post a test event to the topic endpoint → Function fires.
- **Quality gate:** `--repo infra`.

## How to Verify (phase gate)
1. `terraform plan -target=module.event-grid`.
2. (post-apply, with 3.3) `curl` the topic endpoint with a sample Datadog payload → the Function logs receipt.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Apply/verify ⛔ B1; end-to-end needs task 3.3. Code writable now._
