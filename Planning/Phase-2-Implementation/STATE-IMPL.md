# Sentinel Phase 2 — Implementation State

> Live execution state. `/implement-phase` reads this first and updates it after every task.
> Planning-side state (architecture decisions) stays in [../Phase-2/STATE.md](../Phase-2/STATE.md).
>
> Last updated: 2026-07-12

## Current Position

| Field | Value |
|-------|-------|
| **Active category** | 1 — sentinel-infra (not yet started) |
| **Active phase** | 1 — Foundations & Bootstrap |
| **Active branch** | _none — phase not started_ |
| **Active PR** | _none_ |
| **Current task** | 1.1 — Repo skeleton + provider/backend/vars config |
| **Tasks verified** | 0 / 58 |
| **Phases merged** | 0 / 16 |
| **Branch model** | `release-phase-2` → `dev/<cat>-phase-<M>-<slug>` → PR back to `release-phase-2`. `release-phase-2` → `main` once, at the end of Phase 2. See [README §6](README.md#6-git-model--one-branch--one-pr-per-phase). |
| **Tracker commits** | ride the phase's `dev/` branch in `Sentinel`. Infra/deployment phases = 2 branches + 2 PRs (code in the sibling repo, docs here). |

## Next Action

Start **infra Phase 1**. Before task 1.1, clear the Phase-1 prerequisite blockers below —
the Terraform *code* can be written without Azure, but nothing can `plan`/`apply` or be
verified until the Azure account + bootstrap exist. `/implement-phase` will write BLOCKED
reports for any task whose verification needs an unavailable resource.

**Clear first, in this order — these gate the git workflow itself, not just verification:**
1. **B14** — merge the updated `guard-main-source.yml` + `ci.yml` into `release-phase-2` and
   `main` of `Sentinel`. Until then every `dev/*` PR is rejected by the stale guard.
2. **B13** — create + push `release-phase-2` in `Sentinel-infra` and `Sentinel-deployment`.
3. **R3** — confirm the Azure region. It is now a **halting** prerequisite for infra 1.1
   (`/implement-phase` Step 4.1); the agent will not default it to `eastus`.

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
| B10 | Entra `sentinel-db-admins` security group created + set as Postgres Entra admin; SP + backend UAMI added; DB roles created (`pgaadauth_create_principal`) | infra Postgres (Entra-only auth), all DB access | Keshav | open |
| B11 | Backend Entra app registration (`api://sentinel-backend` + `Incident.Write` role) + role grant to `sentinel-gha` SP | backend inbound auth, incident workflow token | Keshav | open |
| B12 | AKS OIDC issuer + workload identity enabled; backend UAMI + federated credential; `sentinel-backend` ServiceAccount annotated | backend runtime KV/DB access (no pod secret) | Keshav | open |
| B13 | `release-phase-2` branch created + pushed in `Sentinel-infra` and `Sentinel-deployment` (both currently have `main` only) | every infra + deployment phase branch/PR | Keshav | open |
| B14 | Updated `guard-main-source.yml` + `ci.yml` merged into `release-phase-2` **and** `main` of `Sentinel` — GitHub reads the guard workflow from the PR's **base** branch, so the `dev/*` allowance is inert until it exists there | every `dev/*` → `release-phase-2` PR in `Sentinel` | Keshav | open |

## Open Reconciliations (decide before the affected task)

| # | Item | Affects | Status / resolution |
|---|------|---------|---------------------|
| R1 | GitHub owner `keshxvDev` in arch docs vs real `Keshav0375`; repo casing | infra 1.3, 4.1, 3.3 | ✅ **RESOLVED 2026-07-11** — all arch docs updated to `Keshav0375` + real repo casing via `/sentinel-planner`; OIDC subjects noted case-sensitive. |
| R2 | Backend repo GitHub name = `Sentinel` (capital S) | infra 4.1, Function bridge dispatch target | ✅ **RESOLVED 2026-07-11** — all three remotes confirmed `Keshav0375/Sentinel-infra`, `.../Sentinel-deployment`, `.../Sentinel`. |
| R3 | Azure region for all resources (arch examples use `eastus`) | all infra modules | **OPEN** — confirm free-tier availability of B2ats_v2 + F1 in chosen region before infra Phase 2/3. |

## Change Log

- **2026-07-26** — **rev-6 build-loop repair.** Audit of architecture ↔ implementation ↔ tasks ↔
  human gate found three defects that would have broken or falsified the first phase. Fixed:
  **(1) Branch model** — phases now branch from `release-phase-2` with a **`dev/`** prefix (was
  `impl/` off `main`) and PR back into `release-phase-2`; `release-phase-2` → `main` is a single
  final merge. `ci.yml` branch-convention accepts `dev`; `guard-main-source.yml` allows
  `release-phase-2 <- dev/* | planning/phase-2-e2e`. Previously **every** phase PR failed two
  required checks by construction. **(2) Quality gate** — `terraform validate` now runs after
  `terraform init -backend=false` (bare `validate` always failed → infra could never go green);
  backend pytest globs extended to `test_memory/`, `test_infra/`, `test_providers/`, `test_eval/`,
  `test_config.py` (backend phases 1, 2 and 6 previously reported green with **zero** of their own
  tests executed); non-existent path args are pruned so the gate is safe mid-build.
  **(3) Tracker commits** — defined: they ride the phase's `dev/` branch in `Sentinel`, so an
  infra/deployment phase is 2 branches + 2 PRs closed at one gate. **(4) Stale rev-5 sweep** —
  infra 1.1/2.2/2.3/4.3 and backend 5.1 bodies reconciled with their own rev-5 callouts; both
  `env-examples/` de-staled (`db_password`, `SENTINEL_API_TOKEN`). **(5) Open R-items** are now a
  halting prerequisite in `/implement-phase` Step 4.1 — R3 (region) can no longer be silently
  defaulted to `eastus`. **(6)** Legacy `/implement` + `/fire` retired, `/review` rewritten for
  Phase-2 invariants. New blockers **B13** (create `release-phase-2` in the two sibling repos) and
  **B14** (guard workflow must reach `release-phase-2` + `main` before it takes effect).
- **2026-07-12** — **rev-5 security + ground-truth overhaul** applied across all architecture
  docs (via `/sentinel-planner`; logged in [../Phase-2/STATE.md](../Phase-2/STATE.md) decision
  log). Four approved changes: (1) `ci_destroy_infra` full-teardown workflow; (2) sentinel-
  deployment pivot to 30 scenario branches (3 cases) — `ci_demo_prs.yml` removed, branches
  become the eval dataset; (3) Azure-native dynamic secrets — Entra-only PostgreSQL, Key Vault
  rotation Function, AKS workload identity; (4) inbound Entra bearer auth on the backend
  (deletes `sentinel-api-token`). New bootstrap blockers **B10–B12** added (Entra DB admin
  group, backend app registration, AKS workload identity). TODO tracker updated with new/
  modified tasks (see TODO.md); task total re-baselined.
- **2026-07-11** — R1 + R2 resolved: architecture docs reconciled from placeholder
  `keshxvDev`/lowercase repos to real `Keshav0375` + `Sentinel-infra`/`Sentinel-deployment`/
  `Sentinel` casing (via `/sentinel-planner`, logged in Phase-2 STATE decision log). All three
  git remotes confirmed. R3 (region) still open.
- **2026-07-11** — Implementation tracker created. Full decomposition: 3 categories, 16
  phases, 55 tasks. Git model set: one branch + one PR per phase, merged after human
  phase-gate; no Claude attribution on commits/PRs.
