# task-3 — Azure Function bridge module (+ Python source)   ·   [category-1-sentinel-infra / phase-3-compute-modules]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Phase branch** | `dev/infra-phase-3-compute-modules` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §3.5; sentinel §2 (correlation_id) |
| **Depends on** | [[task-3-keyvault-module]] (github-pat ref), [[task-2-event-grid-module]] |
| **Referenced by** | [[task-3-ci-incident-response]] (repository_dispatch consumer) |

## Spec
Consumption Function that bridges Event Grid → GitHub `repository_dispatch` on the sentinel
repo, generating `correlation_id` and passing Datadog context through.

**Files created:**
- `modules/functions/{main.tf,variables.tf,outputs.tf}` — `azurerm_service_plan` Y1 Linux, `azurerm_storage_account` for the function, `azurerm_linux_function_app "bridge"` python 3.12, app_settings: `GITHUB_TOKEN=@Microsoft.KeyVault(...github-pat)`, `GITHUB_REPO=Keshav0375/Sentinel` (⚠ R1/R2 — not keshxvDev), `GITHUB_EVENT_TYPE=incident-alert`.
- `modules/functions/src/bridge/{__init__.py,function.json}` — Event Grid trigger; POST to `api.github.com/repos/${GITHUB_REPO}/dispatches` with `event_type` + `client_payload` (inject `correlation_id = uuid4()`, pass `dd_event_id`, `alert_id`, `tags`).
- `outputs.tf` — `function_app_id`, `function_app_name`.

## Prerequisites
- [ ] task 2.3 Key Vault (github-pat). [ ] ⛔ B1 to apply; ⛔ B9 (GitHub PAT) for the secret.
- [ ] Confirm backend repo GitHub name (R2).

## Acceptance Criteria
- [ ] Validates; `GITHUB_REPO` uses real owner/repo; token via Key Vault reference (not literal).
- [ ] Bridge posts a well-formed `repository_dispatch` with `event_type=incident-alert` and `client_payload` carrying correlation_id + dd fields.
- [ ] `function.json` binds the Event Grid trigger.

## Tests
- **Unit:** local test of the bridge handler with a sample Event Grid event → asserts the outgoing GitHub payload shape (mock httpx).
- **Validate:** terraform validate, tflint; `python -m py_compile` the bridge; gitleaks (no PAT literal).
- **Integration (⛔ B1/B9):** deploy, fire a sample event, confirm a `repository_dispatch` reaches the sentinel repo.
- **Quality gate:** `--repo infra`.

## How to Verify (phase gate)
1. Unit test of the handler passes.
2. (post-apply) trigger via Event Grid (task 3.2) → the sentinel repo Actions tab shows an `incident-alert` dispatch received.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Apply/verify ⛔ B1 + B9. Handler code + unit test writable now._
