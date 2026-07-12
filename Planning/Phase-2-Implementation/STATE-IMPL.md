# Sentinel Phase 2 — Implementation State

> Live execution state. `/sentinel-build` reads this first and updates it after every task.
> Planning-side state (architecture decisions) stays in [../Phase-2/STATE.md](../Phase-2/STATE.md).
>
> Last updated: 2026-07-11

## Current Position

| Field | Value |
|-------|-------|
| **Active category** | 1 — sentinel-infra (not yet started) |
| **Active phase** | 1 — Foundations & Bootstrap |
| **Active branch** | _none — phase not started_ |
| **Active PR** | _none_ |
| **Current task** | 1.1 — Repo skeleton + provider/backend/vars config |
| **Tasks verified** | 0 / 55 |
| **Phases merged** | 0 / 16 |

## Next Action

Start **infra Phase 1**. Before task 1.1, clear the Phase-1 prerequisite blockers below —
the Terraform *code* can be written without Azure, but nothing can `plan`/`apply` or be
verified until the Azure account + bootstrap exist. `/sentinel-build` will write BLOCKED
reports for any task whose verification needs an unavailable resource.

## Phase Gate Ledger

A phase moves to `verified` only after the user confirms the feature works and the PR is
merged. Newest first.

| Date | Category | Phase | Branch | PR | Verified by | Notes |
|------|----------|-------|--------|----|-----|-------|
| — | — | — | — | — | — | _no phases completed yet_ |

## Blockers

External dependencies that halt verification. Mirror any task-level BLOCKED here.
(Seeded from [../Phase-2/STATE.md](../Phase-2/STATE.md) blockers — these gate infra Phase 1–4.)

| # | Blocker | Blocks | Owner | Status |
|---|---------|--------|-------|--------|
| B1 | Azure subscription + `sentinel-rg` resource group | All infra apply/verify (§10 bootstrap) | Keshav | open |
| B2 | Terraform state storage bootstrapped (state-rg + storage + container) | infra 1.2 verify, all applies | Keshav | open |
| B3 | OIDC SP + first federated credential created via `az` | infra 1.3 verify | Keshav | open |
| B4 | Anthropic API key | backend LLM calls; Key Vault seed | Keshav | open |
| B5 | OpenAI API key | backend fallback; Key Vault seed | Keshav | open |
| B6 | Datadog account + API key + app key + site | deployment pipeline + monitors; backend fetch_logs | Keshav | open |
| B7 | LangFuse cloud account (public + secret key) | backend tracing | Keshav | open |
| B8 | Microsoft Teams incoming webhook URL | notifications (GHA) | Keshav | open |
| B9 | GitHub PAT (`repo` scope) for cross-repo secret push + Function bridge | infra 4.1, Function bridge | Keshav | open |

## Open Reconciliations (decide before the affected task)

| # | Item | Affects | Status / resolution |
|---|------|---------|---------------------|
| R1 | GitHub owner `keshxvDev` in arch docs vs real `Keshav0375`; repo casing | infra 1.3, 4.1, 3.3 | ✅ **RESOLVED 2026-07-11** — all arch docs updated to `Keshav0375` + real repo casing via `/sentinel-planner`; OIDC subjects noted case-sensitive. |
| R2 | Backend repo GitHub name = `Sentinel` (capital S) | infra 4.1, Function bridge dispatch target | ✅ **RESOLVED 2026-07-11** — all three remotes confirmed `Keshav0375/Sentinel-infra`, `.../Sentinel-deployment`, `.../Sentinel`. |
| R3 | Azure region for all resources (arch examples use `eastus`) | all infra modules | **OPEN** — confirm free-tier availability of B2ats_v2 + F1 in chosen region before infra Phase 2/3. |

## Change Log

- **2026-07-11** — R1 + R2 resolved: architecture docs reconciled from placeholder
  `keshxvDev`/lowercase repos to real `Keshav0375` + `Sentinel-infra`/`Sentinel-deployment`/
  `Sentinel` casing (via `/sentinel-planner`, logged in Phase-2 STATE decision log). All three
  git remotes confirmed. R3 (region) still open.
- **2026-07-11** — Implementation tracker created. Full decomposition: 3 categories, 16
  phases, 55 tasks. Git model set: one branch + one PR per phase, merged after human
  phase-gate; no Claude attribution on commits/PRs.
