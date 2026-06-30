# Sentinel Phase 2 — Planning State

> Last updated: 2026-06-30

## Current Phase

Architecture planning — sentinel-deployment is furthest along (architecture drafted, needs Azure update). Other repos are in early planning. Waiting on Datadog and Azure setup before building.

## Repo Status

| Repo | Architecture | TODO | Testing | README | Status |
|------|-------------|------|---------|--------|--------|
| sentinel | not-started | not-started | not-started | draft | Early planning |
| sentinel-deployment | draft | not-started | not-started | draft | Architecture needs DO→Azure update |
| sentinel-infra | not-started | not-started | not-started | draft | Waiting on other repos |
| sentinel-backend | not-started | not-started | not-started | draft | Waiting on deployment arch |

## Open Decisions

- [ ] sentinel-deployment: Update ARCHITECTURE.md from DigitalOcean to Azure App Service F1 — context: DO requires payment method, Azure F1 is always-free
- [ ] sentinel-deployment: Container deploy vs zip deploy on App Service — context: Docker adds complexity but is more portable; zip deploy is simpler for a 3-endpoint app
- [ ] sentinel-backend: Where to host — context: App Service F1 vs AKS free control plane vs Azure Functions
- [ ] sentinel-backend: PostgreSQL connection strategy — context: migrating from SQLite, need async driver choice
- [ ] sentinel: Event routing architecture — context: how Datadog alerts reach the GHA orchestration layer (Event Grid → Functions → repository_dispatch)
- [ ] sentinel: GHA workflow structure for incident response — context: how many workflows, what triggers, what steps
- [ ] sentinel-infra: Terraform module structure — context: monorepo vs per-resource modules

## Blockers

- [ ] Datadog account setup — who: Keshav — impact: blocks sentinel-deployment pipeline testing and Datadog integration architecture details (DD_SITE, API key)
- [ ] Azure App Service creation — who: Keshav — impact: blocks sentinel-deployment deploy pipeline (need app URL, resource group)

## Decision Log

### 2026-06-30: Deploy target changed from DigitalOcean to Azure App Service
**Context:** DigitalOcean requires a payment method even with $200 student credit. Azure student account is already active with $139 credit.
**Decision:** Use Azure App Service F1 (always-free tier) instead of DO App Platform. F1 gives 60 min CPU/day, 1 GB RAM — more than enough for dummy-api.
**Impact:** sentinel-deployment ARCHITECTURE.md needs rewrite of deploy target sections. reference-documentation/links.md updated with Azure App Service + Datadog Azure integration docs.

### 2026-06-30: Datadog integration strategy — dual path
**Context:** Needed to decide how deployment data reaches Datadog.
**Decision:** Two parallel data paths: (1) Native Azure integration auto-pulls App Service metrics (CPU, memory, HTTP codes). (2) GHA deploy pipeline sends custom events + structured logs via curl to Datadog Events/Log Intake APIs.
**Impact:** reference-documentation/links.md created with full findings from Datadog Azure integration docs.

### 2026-06-30: sentinel-deployment app design — pipeline as product
**Context:** Originally designed as a complex job queue app. Pivoted to make the deployment pipeline the interesting part.
**Decision:** App is near-trivial (GET /, /health, /version). The GHA deploy.yml pipeline (build → push → deploy → verify) generates all Datadog signal. Each stage reports success/failure as Datadog Events. Demo PRs create intentional failures across different stages.
**Impact:** sentinel-deployment ARCHITECTURE.md fully rewritten with new design.

### 2026-06-30: Three-repo architecture
**Context:** Needed to separate concerns for a production-grade system.
**Decision:** Three repos: sentinel (GHA orchestration + planning), sentinel-deployment (dummy app + deploy pipeline), sentinel-infra (Terraform IaC). Backend is currently part of sentinel but will be independently deployable.
**Impact:** Planning/Phase-2/ folder structure created with per-repo subfolders.

### 2026-06-30: Notification channel — Microsoft Teams
**Context:** Need a notification target for incident alerts and deploy status.
**Decision:** Use Microsoft Teams incoming webhook. No Slack.
**Impact:** All architecture docs reference Teams, not Slack.

### 2026-06-30: LLM providers — Groq primary, Gemini backup
**Context:** Need LLM access for agent pipeline without spending Azure credit.
**Decision:** Groq free tier as primary (OpenAI-compatible API), Gemini free tier as backup via google-genai.
**Impact:** Already implemented in Phase 1. No change needed.

### 2026-06-30: CI pipeline structure
**Context:** Needed CI for the sentinel repo itself.
**Decision:** 4 linear GHA jobs: Setup → Branch Convention → Unit Tests (lint, format, typecheck, pytest as steps) → Summary. Only runs on PR to main. Required status check blocks merge.
**Impact:** .github/workflows/ci.yml fully implemented. GitHub ruleset configured.
