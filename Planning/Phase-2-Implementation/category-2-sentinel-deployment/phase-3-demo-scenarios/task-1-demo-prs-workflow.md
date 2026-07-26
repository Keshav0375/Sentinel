# task-1 — 30 scenario branches + `scenarios/branches.yaml` catalog   ·   [category-2-sentinel-deployment / phase-3-scenario-branches]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-deployment` |
| **Phase branch** | `dev/deploy-phase-3-scenario-branches` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-deployment/ARCHITECTURE.md §4 (30 branches, 3 cases) + §4.1 (catalog) |
| **Depends on** | [[task-1-fastapi-app]], [[task-2-ci-app-deployment]], [[task-3-datadog-monitors]] |
| **Referenced by** | backend incident pipeline (signal_type) + eval runner (ground truth) |

> ⚠ **rev-5 (2026-07-12):** supersedes the old `ci_demo_prs.yml` + 14-PR (A/B/C) design.
> The demo app is now **real ground truth**; there is **no PR-faking workflow**. Scenarios
> are **30 real git branches** (10 per case), each with a fixed ground-truth label. The
> branch set doubles as the eval dataset (replaces Phase-1 synthetic scenario JSON).

## Spec
Author 30 scenario branches off `main` and a machine-readable catalog. Each branch is a
self-contained change that, when deployed by `ci_app_deployment.yml`, produces exactly one
of three outcomes.

**Files created:**
- `scenarios/branches.yaml` — one entry per branch: `branch`, `case` (`pass|deployfail|runtime`),
  `fault` (what it injects), `expected_signal_type` (`—|deploy_failure|runtime_error`),
  `expected_resolution` (`none|rollback|rollback_or_escalate`), and `expected_failed_stage`
  for case ii. This file is the **ground truth** the eval runner (backend §6.2) scores against.
- **30 branches** pushed to `Keshav0375/Sentinel-deployment`:
  - `pass/01..10` — trivial safe changes (add `/info`, tweak log line, add field to `GET /`,
    comment bump…). Green deploy, healthy, **no monitor fires** (true negatives).
  - `deployfail/01..10` — deploy breaks (bad requirement, `/health` 503, slow-startup timeout,
    version mismatch, syntax error, bad start command…). Previous version stays live →
    `deploy-failure` event monitor → `signal_type=deploy_failure`.
  - `runtime/01..10` — green deploy, breaks at runtime (`GET /` 500 with verify passing,
    delayed `/health` degradation, memory leak, unhandled exception on a payload…) →
    `runtime-health` monitor → `signal_type=runtime_error`.

No `ci_demo_prs.yml`. No backend involvement in authoring. No `SENTINEL_API_URL`.

## Prerequisites
- [ ] task 1.1 app to mutate. [ ] task 2.2 `ci_app_deployment.yml` exists. [ ] task 2.3 monitors defined.
- [ ] `gh` + repo write to push branches. [ ] Datadog + App Service live (⛔ B6 + Azure) for end-to-end signal.

## Acceptance Criteria
- [ ] `scenarios/branches.yaml` validates (schema) with exactly 30 entries, 10 per case.
- [ ] Every branch exists, applies cleanly on `main`, and matches its catalogued fault.
- [ ] Deploying a `deployfail/*` branch yields `deploy_status:failed` + the catalogued `failed_stage`;
      the previously-deployed version keeps serving.
- [ ] Deploying a `runtime/*` branch passes verify (`/health`+`/version`) but the runtime monitor fires.
- [ ] Deploying a `pass/*` branch stays green and fires no monitor.

## Tests
- **Lint:** `yamllint` on `branches.yaml`; assert each branch diff applies to `main`.
- **Integration:** deploy one per case (`pass/01`, `deployfail/01`, `runtime/01`) → assert the
  Datadog signal + `signal_type` the bridge would stamp (sentinel-infra §3.5).
- **Quality gate:** `--repo deployment`.

## How to Verify (phase gate)
1. `branches.yaml` schema-valid, 30 entries; a dry apply of a sample from each case produces the expected diff.
2. Deploy `deployfail/01` (previous version keeps serving; `deploy_failure` event) and `runtime/01`
   (green deploy, `runtime_error` after verify) → both reproduce the documented Datadog signal.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_End-to-end signal needs Category-2 phase-2 wired + Datadog/App Service live (B6). Branches + catalog writable now._
