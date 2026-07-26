# task-6 — Key Vault rotation: policy + rotator Function   ·   [category-1-sentinel-infra / phase-3-compute-modules]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Phase branch** | `dev/infra-phase-3-compute-modules` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §3.8 (rotation Function) + §3.3 (KV roles) |
| **Depends on** | [[task-3-keyvault-module]], [[task-3-functions-bridge-module]] (shares the Y1 plan) |
| **Referenced by** | backend LLM key reads (always-latest version) |

> **rev-5 (2026-07-12):** new task. Azure Key Vault has **no dynamic secret engine** (that's
> HashiCorp Vault). This implements Azure-native rotation: rotation policy + `SecretNearExpiry`
> event → rotator Function writes a new version. Rotation, not on-demand generation.

## Spec
Rotate the LLM API keys on a schedule with a Function triggered by Key Vault near-expiry events.

**Files created / changed:**
- `modules/keyvault/` — set an expiry on `anthropic-api-key` / `openai-api-key` (seed ~90d);
  document that the rotator owns subsequent versions.
- `modules/functions/`:
  - `azurerm_linux_function_app "rotator"` (system-assigned MI) sharing the bridge's `Y1` plan;
    MI granted `Key Vault Secrets Officer` (in [[task-3-keyvault-module]]).
  - `azurerm_eventgrid_system_topic "kv"` (source = the vault) + system-topic event subscription
    for `Microsoft.KeyVault.SecretNearExpiry` → the rotator Function.
  - `src/rotate/` (Python): read secret name from the event → call the provider key-management
    API to mint a fresh key (Anthropic Admin API where org-admin available; else post a Teams
    "manual rotation required" alert) → `set_secret` a new version with a fresh 90-day expiry.

**Contract:**
```
trigger   = Microsoft.KeyVault.SecretNearExpiry (default 30d before expiry)
writes    = new secret VERSION (backend reads latest via workload identity — no redeploy)
identity  = rotator system MI → Key Vault Secrets Officer
```

## Prerequisites
- [ ] Key Vault + functions plan exist (tasks 2.3, 3.3). [ ] `az` rights; Anthropic Admin API access (optional).

## Acceptance Criteria
- [ ] `terraform validate`/`tflint` pass; system topic + subscription + Function plan cleanly.
- [ ] Setting a short expiry on a test secret fires `SecretNearExpiry` → Function runs → new version appears.
- [ ] Rotator degrades gracefully (Teams alert) when no provider key API is available.

## Tests
- **Validate:** `terraform validate`, `tflint`; Function unit test for the rotate handler (mock KV + provider).
- **Integration (blocked on B1):** set expiry +1d on a dummy secret → assert a new version within ~15 min.
- **Quality gate:** `--repo infra`.

## How to Verify (phase gate)
1. Deploy; set a near-term expiry on a throwaway secret; confirm the Function fires and writes a new version.
2. Confirm the backend reads the new version without redeploy (via workload identity).

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Verification BLOCKED on B1 (Azure). Terraform + Function source writable now. Anthropic Admin API optional._
