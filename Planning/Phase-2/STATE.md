# Sentinel Phase 2 — Planning State

> Last updated: 2026-07-04

## Current Phase

Architecture planning complete for all three repos. All design questions resolved. sentinel-infra architecture expanded with OIDC, cross-repo secrets, Key Vault policies, PostgreSQL firewall, and standardized workflow naming. Ready for implementation once Azure resources are bootstrapped.

## Repo Status

| Repo | Architecture | TODO | Testing | README | Status |
|------|-------------|------|---------|--------|--------|
| sentinel (= backend) | **final** | not-started | not-started | draft | Architecture finalized — all 9 design questions resolved |
| sentinel-deployment | final | not-started | not-started | draft | Ready to build (blocked on Azure + Datadog setup) |
| sentinel-infra | **final** | not-started | not-started | draft | Architecture finalized — 6 modules, AKS removed, secrets updated |

Note: There is no separate sentinel-backend repo. The sentinel repo IS the backend.

## Open Decisions

None — all architectural decisions resolved. Implementation can begin once blockers are cleared.

## Blockers

- [ ] Datadog account setup — who: Keshav — impact: blocks sentinel-deployment pipeline testing, DD_SITE, API key
- [ ] Azure resource group creation — who: Keshav — impact: blocks all infra provisioning and backend deployment
- [ ] LangFuse account creation — who: Keshav — impact: blocks tracing integration (need public key + secret key)
- [ ] Anthropic API key — who: Keshav — impact: blocks all LLM calls (default provider)
- [ ] OpenAI API key — who: Keshav — impact: blocks fallback LLM calls

## Decision Log

### 2026-07-04: Added ci_validation.yml — fast PR gate, split from ci_backend_validation
**Context:** `ci_backend_validation.yml` runs the full pipeline including ACR push, ACR cleanup, and smoke tests. This is heavy for every PR open/sync (~8-12 min). Developers need fast feedback on PRs.
**Decision:** New `ci_validation.yml` runs on PR open/sync: lint (ruff) → typecheck (pyright) → Docker build (local, no push) → run backend from local image → validate /health + /ready → unit tests → integration tests → teardown. No ACR interaction, no smoke test. `ci_backend_validation.yml` moves to `push` trigger (runs on merge to main only) and remains the authoritative validation that pushes to ACR and runs the smoke test.
**Impact:** 7 total workflows across repos (was 6). PR feedback: ~3-5 min. Post-merge full validation: ~8-12 min. Two-stage pipeline: fast gate on PR, full validation on merge.

### 2026-07-04: Merged ci_backend + cd_backend → ci_backend_validation
**Context:** Had two separate workflows: `ci_backend.yml` (quality gate on PR) and `cd_backend.yml` (build+push on merge). But tests should run against the real Docker image, not a local install. And pushing should happen as part of validation, not as a separate post-merge step.
**Decision:** Single `ci_backend_validation.yml` on every PR: lint → typecheck → build + push image to ACR → run backend from the real image → validate /health + /ready → run unit tests → run integration tests → smoke test → teardown. ACR cleanup keeps only the 3 most recent image tags. No separate CD workflow needed.
**Impact:** `cd_backend.yml` removed. Tests now validate the exact artifact that `ci_incident_response.yml` pulls during incidents.

### 2026-07-04: Workflow naming convention — standardized across all repos
**Context:** Workflow file names and display names were inconsistent across sentinel, sentinel-infra, and sentinel-deployment (e.g., `deploy.yml`, `plan.yml`, `incident_response.yml`).
**Decision:** Standard `ci_` prefix convention. Workflow `name:` follows `[repo] scope — description`. Job IDs: `kebab-case` verb-noun. Applied across all three repos.
**Impact:** All workflows: `ci_validation.yml` (fast PR gate), `ci_backend_validation.yml` (full post-merge validation), `ci_incident_response.yml` (real pipeline), `ci_app_deployment.yml` (sentinel-deployment), `ci_infra_dry.yml` (TF validate+plan), `ci_infra.yml` (TF apply), `ci_runners.yml` (runner images).

### 2026-07-04: OIDC workload identity federation — no stored Azure secrets
**Context:** Original plan stored `AZURE_CLIENT_SECRET` as GitHub repo secret. Secrets expire, must be rotated.
**Decision:** OIDC federation — GitHub proves identity via JWT, Azure trusts it. One Azure AD app with federated credentials for all three repos (sentinel-infra main+PR, sentinel main+PR, sentinel-deployment main). Terraform provisions the federated credentials. Only 3 non-secret values needed per repo: CLIENT_ID, TENANT_ID, SUBSCRIPTION_ID.
**Impact:** Added OIDC section (§4) to sentinel-infra ARCHITECTURE.md with full HCL and bootstrap sequence. Removed AZURE_CLIENT_SECRET from all repos.

### 2026-07-04: Cross-repo secret distribution — Terraform pushes GitHub secrets
**Context:** After `terraform apply`, sentinel and sentinel-deployment repos need ACR creds and OIDC IDs as GitHub secrets. Previously manual copy-paste.
**Decision:** Terraform uses `github_actions_secret` resource (GitHub provider) to automatically push ACR_LOGIN_SERVER, ACR_USERNAME, ACR_PASSWORD, AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID to sentinel repo, and OIDC IDs to sentinel-deployment repo. Requires GITHUB_PAT with `repo` scope as sentinel-infra secret.
**Impact:** Added §5 to sentinel-infra ARCHITECTURE.md. GITHUB_PAT added to sentinel-infra repo secrets table.

### 2026-07-04: Key Vault dual access — Terraform writes, GHA reads
**Context:** Key Vault had one access policy (Terraform SP). But incident_response.yml needs to read secrets at runtime.
**Decision:** Two roles: Terraform SP gets `Key Vault Secrets Officer` (create/update secrets). GHA OIDC SP gets `Key Vault Secrets User` (read-only). Switched from access policies to RBAC authorization (`enable_rbac_authorization = true`).
**Impact:** Updated keyvault module (§3.3) with dual role assignments. Added full secret flow diagram showing Key Vault → GHA → Docker env vars.

### 2026-07-04: PostgreSQL firewall — allow all for dev
**Context:** PostgreSQL firewall only allowed Azure services (`0.0.0.0/0.0.0.0`). But ephemeral backend runs on GitHub-hosted runners (not Azure services). GitHub runner IPs rotate across a wide, unpredictable CIDR range.
**Decision:** Allow all (`0.0.0.0` to `255.255.255.255`) for dev. DB still protected by username + password. Production would use Private Endpoint + VNet.
**Impact:** Updated postgresql module (§3.2) with explicit rationale and comparison table of alternatives.

### 2026-07-02: Ephemeral backend — run inside GHA, not AKS
**Context:** Backend was planned for 24/7 AKS hosting. But the pipeline runs a few times per day during demos. Paying for always-on compute for a 5-minute job is waste.
**Decision:** Backend runs as an ephemeral Docker container inside the GHA runner. Per-incident lifecycle: fetch secrets → pull image from ACR → docker run → validate /health + /ready → run pipeline → teardown. Zero compute cost (uses GHA free minutes). PostgreSQL stays always-on (free tier). AKS module removed from Terraform.
**Impact:** Rewrote sentinel/ARCHITECTURE.md §1 (system overview), §8 (K8s manifests → ephemeral lifecycle), §9 (workflows), §10 (Terraform: 7→6 modules), §14 (cost breakdown). No K8s manifests needed. cd_backend.yml simplified to build+push only.

### 2026-07-02: LLM providers — Anthropic + OpenAI only, no Groq/Gemini
**Context:** Original plan used Groq free tier (llama models). But this is a portfolio project — decision-making quality matters more than cost.
**Decision:** Anthropic (default) + OpenAI (fallback). Top models: Sonnet 4.6 for reasoning tasks, Haiku 4.5 for classification. Fallback: gpt-4o / gpt-4o-mini. Switch via `SENTINEL_PRIMARY_PROVIDER` env flag. Both API keys always required. Cost: ~$5-12/month for LLM calls.
**Impact:** Updated sentinel/ARCHITECTURE.md §4.7 (model table), §12 (prerequisites), §14 (cost). Groq and Gemini references removed.

### 2026-07-01: HITL gate = GitHub PR review, not custom API
**Context:** Original design had a `POST /approvals/{incident_id}` endpoint. But the destructive action IS a revert PR — GitHub already has review/approve/merge.
**Decision:** No `/approvals` endpoint. The revert PR on sentinel-deployment IS the HITL surface. Backend decides rollback vs escalate, GHA creates the PR, reviewer merges or closes. `approvals` table renamed to `revert_prs` — tracks PR lifecycle (created, merged, closed).
**Impact:** Removed `/approvals` endpoint from API contracts. Updated pipeline flow, DB schema, tool table. Cleaner separation: backend reasons, GHA executes.

### 2026-07-01: Backend reasons, GHA executes — separation of concerns
**Context:** Who creates the PR? Who sends the Teams notification? Originally the backend did everything.
**Decision:** Backend returns a detailed response with `resolution_type` (rollback/escalated) + full context. GHA jobs branch on that: rollback path → generate-pr-content job → create-rollback-pr job → notify-rollback job. Escalation path → notify-escalation job. Both → summary job. `draft_rollback_pr` tool removed from backend, replaced by `prepare_rollback_spec` (outputs target SHA + justification, doesn't call GitHub API).
**Impact:** Updated incident_response.yml with full job DAG. Updated tool table. Notifications moved entirely to GHA.

### 2026-07-01: PR content generation endpoint — specialized agent for demo PRs
**Context:** sentinel-deployment creates demo PRs that intentionally break things. PR titles and descriptions need to look realistic — a template can't produce convincing developer-style justifications for changes that are secretly broken.
**Decision:** New `POST /generate/pr-content` endpoint on sentinel backend. Takes scenario context (files changed, diff summary, failure class) and returns LLM-generated PR title + description. Uses Haiku 4.5 (constrained generation, not reasoning). sentinel-deployment GHA calls this in a `generate-pr-content` job, passes output to a `create-pr` job.
**Impact:** Updated sentinel/ARCHITECTURE.md §3.4 (new API contract), §4.7 (model table), §13.3 (new files). New files: `agents/pr_content_generator.py`, `agents/prompts/pr_content_generator.txt`, `api/generate.py`.

### 2026-07-01: Phase 1 → Phase 2 cleanup plan — data/, generator/, SQLite removal
**Context:** Phase 1 `data/` directory (10 synthetic scenarios, seed JSON, seed.py, sentinel.db) and `src/sentinel/generator/` (alert_gen, log_gen, deploy_gen, scenarios) exist only to fake data. Phase 2 gets real data from Datadog, GitHub, PostgreSQL.
**Decision:** Full cleanup section added to architecture (§13). Delete: `data/`, `generator/`, `db.py`, `scripts/run_scenario.py`, `scripts/run_eval.py`, `scripts/demo.py`. Rewrite: all 6 tools, memory layer, orchestrator, eval. New: `alembic/` for migrations, `database.py` for asyncpg pool. Migration order: add deps → create new modules → rewrite tools → rewrite orchestrator → delete Phase 1 artifacts (late, to keep Phase 1 working until Phase 2 is proven).
**Impact:** Updated sentinel/ARCHITECTURE.md §1 (diagram), §7 (Dockerfile), added §13 (cleanup plan).

### 2026-07-01: LLM model selection — differentiated per agent role
**Context:** All agents were using the same model. Classification tasks (triage, judge, reflexion, reverification) don't need the same reasoning power.
**Decision:** Haiku 4.5 for classification/scoring tasks. Sonnet 4.6 for reasoning tasks (analysis, resolution, orchestrator). Fallback: OpenAI gpt-4o / gpt-4o-mini. *(Superseded by 2026-07-02 provider decision — Groq/Gemini removed.)*
**Impact:** Better model-task fit. Updated sentinel/ARCHITECTURE.md §4.7.

### 2026-07-01: Agentic loop patterns — reflexion + plan-execute + reverification
**Context:** Linear chain (triage → analysis → resolution → judge) doesn't self-correct. If analysis is wrong, the whole pipeline proceeds on a bad hypothesis.
**Decision:** Three patterns integrated: (1) Plan-execute — orchestrator plans before dispatching. (2) Reflexion — self-critique after analysis, loops back if confidence < 0.7, max 2 loops. (3) Reverification — post-resolution check that the fix matches the root cause.
**Impact:** Pipeline can now self-correct. Tool-call budget raised from 15 to 20 to accommodate loops. Updated sentinel/ARCHITECTURE.md §4.2-4.4.

### 2026-07-01: GHA incident_response.yml — parallel data gathering + confidence-gated execution
**Context:** Original design had GHA just doing `curl POST /webhooks/incident`. Missed opportunity to pre-fetch data in parallel.
**Decision:** New 6-stage workflow: (1) fetch secrets from Key Vault, (2) parallel jobs: service metadata, PR details, Datadog logs, (3) trigger backend with enriched payload, (4) poll for completion + confidence check, (5a) escalate if low confidence, (5b) execute if high confidence, (6) always notify Teams + report to Datadog.
**Impact:** Agent pipeline starts with pre-fetched context — faster resolution. GHA does the fan-out/fan-in, backend does the reasoning. Updated sentinel/ARCHITECTURE.md §9.4.

### 2026-07-01: Concurrent incident handling — asyncio task isolation
**Context:** What if two incidents fire simultaneously?
**Decision:** Each pipeline run = independent `asyncio.Task` with its own ShortTermMemory dict and LangFuse trace. No locking needed — incidents are independent. asyncpg pool (min=2, max=10) handles concurrent DB access. Ephemeral container handles 2-3 concurrent runs within a single incident workflow.
**Impact:** Updated sentinel/ARCHITECTURE.md §4.5.

### 2026-07-01: Incident ID correlation — deployments table + correlation map
**Context:** No way to trace from incident → exact PR → exact deploy → exact Datadog logs.
**Decision:** (1) New `deployments` table linking service, PR number, commit SHA, GHA run ID, Datadog event ID, and incident ID. (2) `correlation_id` generated at Event Grid bridge, carried through GHA → backend → all agents. (3) Every incident links to alert_id, dd_event_id, correlation_id, deploy_id, and langfuse_trace_id.
**Impact:** Full traceability. Given any incident, trace back to exact PR, deploy, logs, and LLM trace. Updated sentinel/ARCHITECTURE.md §2.1, §5.3.

### 2026-07-01: PostgreSQL usage — 4 tables with clear purposes
**Context:** Schema had 3 tables. Missing deploy tracking.
**Decision:** 4 tables: incidents (episodic memory), services (semantic memory), approvals (HITL audit), deployments (deploy ↔ incident correlation). Added columns: dd_event_id, correlation_id, root_cause_confidence, resolution_type, reflexion_loops, mttr_seconds, langfuse_trace_id.
**Impact:** Updated sentinel/ARCHITECTURE.md §5.2-5.4.

### 2026-07-01: LangFuse — full integration (not just tracing)
**Context:** Original plan used LangFuse only for LLM call logging.
**Decision:** Full integration: (1) Tracing — every pipeline run as a trace with spans. (2) Prompt management — load system prompts from LangFuse with local fallback. (3) Scoring — judge scores piped to LangFuse for dashboard. (4) Cost tracking — token usage per agent. (5) Datasets — export trajectories for regression testing.
**Impact:** Added new section sentinel/ARCHITECTURE.md §6. Prompt loading pattern changes from file-only to LangFuse-first.

### 2026-07-01: Terraform justification — documented
**Context:** Why Terraform over Bicep, CLI scripts, or manual?
**Decision:** Terraform wins on: reproducibility, state tracking, provider-agnostic (interview value), PR-reviewable infra. Provisioning 7 modules covering all Azure resources.
**Impact:** Added sentinel/ARCHITECTURE.md §10.

### 2026-07-01: sentinel architecture marked final
**Context:** All 9 deep design questions resolved.
**Decision:** Architecture doc updated to final status. All sections complete.

### 2026-07-01: Ingress — AKS public IP for dev
**Context:** Need external access to backend for webhooks.
**Decision:** Use AKS-assigned public IP for dev. No custom domain needed yet. Can add later.

### 2026-07-01: GHA orchestration — thin HTTP call to backend
**Context:** repository_dispatch workflow needs to trigger the agent pipeline.
**Decision:** Superseded by incident_response.yml design (see above). Now a full multi-job workflow, not just a curl.

### 2026-07-01: Infra bootstrap — manual one-time setup
**Context:** Terraform state storage must exist before first `terraform apply`.
**Decision:** Manually create state resource group + storage account + container. Documented in sentinel-infra ARCHITECTURE.md §5.1. Not a recurring concern.

### 2026-07-01: sentinel = backend (no separate repo)
**Context:** Originally planned sentinel-backend as a separate repo. Realized the sentinel repo already contains all backend code in `src/sentinel/`.
**Decision:** sentinel repo is the backend. All agent code, CI/CD workflows, and deployment configs live here. No separate backend repo.
**Impact:** Removed `Planning/Phase-2/sentinel-backend/` folder. Merged backend architecture into `Planning/Phase-2/sentinel/ARCHITECTURE.md`. Simplified from 4 planning folders to 3.

### 2026-07-01: Backend hosting — AKS with B2ats free node *(SUPERSEDED 2026-07-02: ephemeral backend)*
**Context:** Needed always-on hosting for webhook-driven agent API. App Service F1 sleeps and has 60 min CPU/day limit.
**Decision:** ~~AKS~~ → Superseded by ephemeral Docker container inside GHA runner. See 2026-07-02 entry.
**Impact:** AKS module removed from Terraform. K8s manifests removed. cd_backend.yml simplified to build+push only.

### 2026-07-01: Database — PostgreSQL B1MS + pgvector
**Context:** Phase 1 uses SQLite. Need vector similarity for episodic memory search.
**Decision:** Azure PostgreSQL B1MS (free 12 months, 32 GB) with pgvector extension. Driver: asyncpg. Migrations: alembic.
**Impact:** Schema defined in sentinel/ARCHITECTURE.md. sentinel-infra PostgreSQL module spec'd.

### 2026-07-01: CI/CD — Four workflows for sentinel
**Context:** Need quality gates, auto-deploy, and real incident pipeline.
**Decision:** Four workflows: (1) ci_backend.yml — lint, typecheck, unit + integration tests with pgvector, Docker build verify on PR. (2) cd_backend.yml — build, push to ACR (no deploy — pulled on demand). (3) ci_incident.yml — agent pipeline smoke test on PR. (4) incident_response.yml — full lifecycle with ephemeral backend.
**Impact:** Workflow structure defined in sentinel/ARCHITECTURE.md §9.

### 2026-07-01: CI runner images in ACR
**Context:** Installing ruff, pyright, pytest, az cli, kubectl every CI run is slow.
**Decision:** Build custom ci-runner Docker image with all tools pre-installed, store in ACR, use as `container:` in GHA workflows.
**Impact:** sentinel-infra manages runner image builds. ci-images/ directory with Dockerfiles.

### 2026-07-01: sentinel-infra — per-resource Terraform modules
**Context:** Needed to decide how to structure Terraform code.
**Decision:** One module per Azure resource concern (acr/, postgresql/, keyvault/, event-grid/, functions/, app-service/). Root main.tf wires them together. *(AKS module removed 2026-07-02.)*
**Impact:** sentinel-infra/ARCHITECTURE.md written with full module breakdown and HCL.

### 2026-07-01: sentinel-deployment — zip deploy, not Docker
**Context:** Azure App Service F1 does not support container deploys.
**Decision:** Zip deploy via `az webapp deploy`. Pipeline simplifies to 3 stages. No Dockerfile needed.
**Impact:** sentinel-deployment ARCHITECTURE.md rewritten. Cost: $0/month.

### 2026-07-01: sentinel-deployment architecture finalized
**Context:** All decisions resolved for sentinel-deployment.
**Decision:** Architecture doc marked final.
**Impact:** Unblocks sentinel-infra App Service module.

### 2026-06-30: Deploy target — Azure App Service F1 (not DigitalOcean)
**Context:** DigitalOcean requires payment method.
**Decision:** Azure F1 always-free tier.
**Impact:** All DO references removed.

### 2026-06-30: Datadog integration — dual path
**Context:** How deployment data reaches Datadog.
**Decision:** Native Azure integration (auto metrics) + GHA pipeline curl (custom events/logs).

### 2026-06-30: App design — pipeline as product
**Context:** Pivoted from complex job queue to deployment pipeline as the interesting part.
**Decision:** Trivial app, GHA deploy pipeline generates all Datadog signal.

### 2026-06-30: Three-repo architecture
**Decision:** sentinel (backend + orchestration), sentinel-deployment (dummy app), sentinel-infra (Terraform).

### 2026-06-30: Notification channel — Microsoft Teams

### 2026-06-30: LLM providers — Groq primary, Gemini backup *(SUPERSEDED 2026-07-02: Anthropic + OpenAI)*

### 2026-06-30: CI pipeline — 4 workflows total (updated from original 4-linear-jobs plan)
