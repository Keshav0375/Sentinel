# Sentinel Phase 2 — Planning State

> Last updated: 2026-07-11

## Current Phase

Architecture planning complete for all three repos (rev 4 — 2026-07-05). Backend runs on AKS with **scale-to-zero** (node pool 0↔1 per run, `sentinel-backend` concurrency group, `KEEP_WARM` demo mode, nightly auto-down) and a **dynamic per-run backend URL** (resolved from the cluster; Service + LB IP deleted at teardown → $0 infra at idle). HITL is fire-and-forget, `deployments` table written by the deploy pipeline, demo PRs are template-based (`/generate/pr-content` serves rollback PRs only). Reusable composite actions (`backend-up/down`, `get-kv-secrets`, `notify-teams`, `psql-exec`, `dd-report`) and top-level repo buckets (`src/sentinel`, `azure/`, `.github/`, `Planning/`, `.claude/`, `tests/`, `alembic/`) documented. All four architecture docs + READMEs synced. Ready for implementation once Azure resources are bootstrapped.

## Repo Status

| Repo | Architecture | TODO | Testing | README | Status |
|------|-------------|------|---------|--------|--------|
| sentinel (= backend) | **final (rev 3)** | not-started | not-started | draft | AKS scale-to-zero, fire-and-forget HITL, 3-table schema, ci_backend_scale.yml, /generate/pr-content = rollback PRs only |
| sentinel-deployment | **final (rev 3)** | not-started | not-started | draft | Record-deployment stage; demo PRs template-based, 3-condition taxonomy (A/B/C) + 2 monitors (blocked on Azure + Datadog setup) |
| sentinel-infra | **final (rev 3)** | not-started | not-started | draft | 7 modules — AKS with node_count ignore_changes for scale-to-zero; sentinel-api-token secret |

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

### 2026-07-11: GitHub owner/repo reconciliation — keshxvDev → Keshav0375, real repo casing
**Context:** The architecture docs were written against a placeholder GitHub owner `keshxvDev`
with lowercase repo names (`sentinel`, `sentinel-infra`, `sentinel-deployment`). The actual
cloned repos are `Keshav0375/Sentinel-infra`, `Keshav0375/Sentinel-deployment`, and
`Keshav0375/Sentinel` (capitalized). This matters for correctness, not just cosmetics: the
GitHub OIDC token `sub` claim carries the repo's canonical owner/name, and Azure matches
federated-credential subjects by exact, case-sensitive string — a lowercase or wrong-owner
subject silently fails auth.
**Decision:** Replace every `keshxvDev` reference with `Keshav0375` and correct repo casing
across the architecture docs: OIDC federated-credential subjects (§4.2) + the manual bootstrap
JSON (§4.3), the Function bridge `GITHUB_REPO` (§3.5), the `github` provider `owner` (§5.1),
the `github_actions_secret` `repository` args (§5.2/§5.3), and the cross-repo `uses:` action
references in all three arch docs + the incident-workflow YAML in sentinel §9.3. Added a
casing-sensitivity note to sentinel-infra §4.2. The historical decision-log entry (2026-07-05)
that shows `<owner>/sentinel/...` is left as-is (history); new work uses the concrete values.
**Impact:** sentinel-infra ARCHITECTURE §3.5, §4.2, §4.3, §5.1–5.3; sentinel ARCHITECTURE §9.3;
sentinel-deployment ARCHITECTURE §3.2; master §8. Implementation tracker R1/R2 resolved.

### 2026-07-05: Dynamic backend URL + reusable composite actions + repo buckets
**Context:** The static `SENTINEL_BACKEND_URL` variable forced the LB public IP to persist at scale-zero (~$3-4/mo) — and after the demo-PR correction, no consumer outside the two GHA workflows needs a stable URL. Separately, repeated step blocks (Key Vault reads, Teams posts, psql calls) were copy-pasted across jobs.
**Decision:** (1) **Dynamic URL:** `backend-up` resolves the LB ingress IP from the cluster and emits `backend-url` as an action/job output (non-secret → outputs are legal); `backend-down` deletes the Service, releasing the public IP → **infra is $0 at idle**. Fresh IP per cold run (+~1-3 min LB provisioning); same IP within a KEEP_WARM session. `SENTINEL_BACKEND_URL` variable removed. (2) **Reusable composite actions** in sentinel `.github/actions/`: `backend-up`, `backend-down`, `get-kv-secrets` (fetch + mask), `notify-teams`, `psql-exec`; local `dd-report` action in sentinel-deployment; cross-repo reuse via `uses: <owner>/sentinel/.github/actions/...@main`. Rule: any step block used twice becomes an action. (3) **Repo buckets** documented (sentinel §1.3): `src/sentinel/` (all reasoning, incl. providers/), `azure/` (k8s manifests — moved from `k8s/`), `.github/` (workflows + actions), `alembic/`, `tests/`, `Planning/`, `.claude/` — file system mirrors "backend reasons, GHA executes."
**Impact:** sentinel §1.1/§1.3 (new)/§8.1/§8.5/§9/§13/§14. Master, infra cost + KV flow, deployment §3.2/§3 Stage 5/§5. Total cost now ~$5-12/mo (LLM only), $0 infra at idle.

### 2026-07-05: Scale-to-zero backend — node pool 0↔1 per run (amends AKS always-on)
**Context:** Always-on consumes ~730 of the 750 free B2ats_v2 hrs/mo, leaving nothing for other projects. User chose per-run up/down over the recommended session toggle, accepting the cold-start cost. Key insight driving the design: the **node** is the meter, not the pod — scale-to-zero must target the node pool.
**Decision:** Node pool idles at 0. Shared composite actions `backend-up` (nodepool→1, replicas→1, wait /ready; ~3-7 min cold) and `backend-down` (both→0). `ci_incident_response.yml`: `ensure-backend-up` replaces the health gate (can't come up → Teams alert + loud fail); `teardown-backend` before summary. `ci_backend_deployment.yml`: scales up before deploy, down after. Safety rails: (1) both workflows serialized via `concurrency: group: sentinel-backend` so one run's teardown can't kill another's backend (GHA caveat documented: max 1 queued run per group); (2) `SENTINEL_KEEP_WARM=true` repo variable skips teardowns for live demo sessions; (3) new `ci_backend_scale.yml` — manual up/down + nightly auto-down cron; (4) Terraform `ignore_changes` on node_count so applies don't fight the workflows. LB IP persists at scale-zero → `BACKEND_URL` stays stable. Node consumption: ~20-80 hrs/mo; backend-computed MTTR excludes spin-up (measured from webhook receipt).
**Impact:** sentinel ARCHITECTURE §1, §8.4/§8.5 (new), §9.2/§9.3/§9.5, §14. sentinel-infra §3.7 (lifecycle block), §11. Master doc, READMEs synced.

### 2026-07-05: /generate/pr-content scope — rollback PR content ONLY; demo PRs are template-based
**Context:** Docs had sentinel-deployment's demo-PR workflow calling the backend for realistic PR titles/descriptions. User correction: demo PRs are manual/automated template-style with no agent involvement; the PR-content agent exists solely to write the revert PR during incident response.
**Decision:** `ci_demo_prs.yml` is self-contained — each scenario carries scripted file changes + a pre-written title/description; no backend call, no `SENTINEL_API_URL` variable, not in the concurrency group. `POST /generate/pr-content`'s only consumer is `ci_incident_response.yml`'s `generate-pr-content` job; its contract now takes incident context (root_cause, evidence_summary, confidence, target_deploy) and returns a `revert:`-prefixed title + Incident/Root Cause/Evidence/Rollback description written for the merge-or-close reviewer.
**Impact:** sentinel ARCHITECTURE §3.4 rewritten, §1.1/§8.4/§8.5 cleaned. sentinel-deployment §4/§3.4/§5/§8, README. Infra: sentinel-api-token consumer list.

### 2026-07-04: ci_incident_response health gate — validate the backend, never start it *(SUPERSEDED 2026-07-05: scale-to-zero — `ensure-backend-up` scales the backend up instead of gate-and-fail; the Teams-alert-on-failure behavior carries over)*
**Context:** With the backend always-on on AKS, the incident workflow must not manage its lifecycle — but it must also never process an incident against a dead backend, and never fail silently.
**Decision:** New `check-backend` gate job (Job 0): `curl /health` + `/ready`. Ready → proceed to parallel context fetch. Down → Teams alert ("incident alert received but backend is unreachable — NOT processed, manual action needed") and the workflow fails loudly. All fetch jobs `needs: check-backend`.
**Impact:** sentinel ARCHITECTURE §9.3 (job flow + YAML), §9.5, master §2/§8, sentinel README.

### 2026-07-04: Demo PR taxonomy — three conditions (A/B/C) + two Datadog monitors
**Context:** The 10-PR demo sequence only produced pipeline-detected failures (verify/deploy stage). It had zero scenarios where the deploy is green but the site breaks at runtime — the condition that exercises Sentinel's real diagnostic value.
**Decision:** Every demo PR is classified: **A** clean (green deploy, healthy app, no incident — baseline memory); **B** runtime failure (green deploy, app breaks under traffic → runtime-health monitor fires → revert PR heals the live site); **C** deploy failure (pipeline red, old version usually keeps serving → deploy-failure event monitor fires → revert PR heals main's deployability). Added PRs #11-14 (B scenarios: break `GET /` while `/health` stays green; delayed `/health` degradation past the verify window). Two Datadog monitors defined: `sentinel-deploy-failure` (event monitor on `deploy_status:failed`) and `sentinel-runtime-health` (5xx rate / failed health pings), both notifying the same webhook → Event Grid path; alert tags tell the agents which evidence class to weigh.
**Impact:** sentinel-deployment ARCHITECTURE §4 (taxonomy + 14-PR table), new §6.3 (monitors), README. Partially resolves the "monitor definition missing" review gap.

### 2026-07-04: Backend hosting — AKS single replica (supersedes 2026-07-02 ephemeral decision) *(AMENDED 2026-07-05: scale-to-zero — no longer always-on)*
**Context:** Architecture review found the ephemeral design unimplementable as specced: each GHA job runs on a fresh runner VM, so a container started in a `start-backend` job doesn't exist for `run-agent-pipeline`; GHA also refuses to pass masked secrets between jobs via outputs. The ephemeral pivot had additionally orphaned `/generate/pr-content` (sentinel-deployment's demo-PR workflow had no host to call) and removed the always-on API.
**Decision:** Backend runs as a single-replica Kubernetes Deployment on AKS — free control plane + 1× B2ats_v2 node (free 750 hrs/mo for 12 months, expires 05/2027). Public LoadBalancer IP (dev); all non-health endpoints require `X-Sentinel-Token` (new Key Vault secret `sentinel-api-token`). Secrets sync Key Vault → K8s Secret at deploy time. Kubelet gets AcrPull. K8s manifests (`k8s/deployment.yaml`, `k8s/service.yaml`) live in the sentinel repo, applied by CI — Terraform provisions the cluster only.
**Impact:** sentinel ARCHITECTURE §1, §8 (ephemeral lifecycle → AKS deployment), §9 (all three workflows redesigned), §14 (cost). sentinel-infra: AKS module restored (6→7 modules), secret flows rewritten. Master doc synced. Cost: still ~$0 infra for 12 months (+~$0-4 LB IP); ~$30/mo node after.

### 2026-07-04: HITL — fire-and-forget PR creation, no lifecycle tracking
**Context:** The old design said the backend "watches for the PR merge event" — impossible-adjacent and unnecessary; Phase 2 has no requirement for reviewer/merge-outcome data.
**Decision:** Sentinel's job ends when the revert PR exists and Teams is notified (or escalation is notified). No waiting states, no merge/close tracking. `revert_prs` table dropped (4→3 tables); `pr_number`/`pr_url` recorded on the `incidents` row by GHA right after PR creation (creation record only). Incident is terminal at pipeline completion; `mttr_seconds` = pipeline duration (alert → decision), not human review latency. The PR on GitHub remains the approval gate and audit trail.
**Impact:** sentinel ARCHITECTURE §3.3, §5 (schema), §9.3. Master §5, §7. READMEs synced.

### 2026-07-04: deployments table writer — ci_app_deployment.yml records every deploy
**Context:** Review found the `deployments` table had no writer — `get_deploy_details` and deploy↔incident correlation would query an empty table.
**Decision:** New Stage 5 "Record Deployment" in ci_app_deployment.yml, `if: always()`: OIDC login → read `db-password` from Key Vault → psql INSERT (service, pr_number, commit_sha, author, deploy_status, gha_run_id, files_changed, metadata incl. failed_stage). Failed deploys recorded too — they're the rows incidents join against. `incident_id` backfilled by the backend on correlation. No DB credentials in GitHub secrets.
**Impact:** sentinel-deployment ARCHITECTURE §3 (stages renumbered), §3.3, §3.4. PostgreSQL firewall rationale updated (GHA runners write to DB).

### 2026-07-04: Renamed ci_backend_validation.yml → ci_backend_deployment.yml
**Context:** With AKS hosting the post-merge workflow's job is deployment, not just validation.
**Decision:** `ci_backend_deployment.yml`: quality → build+push (immutable sha tag, no `latest`) → deploy-to-aks (secret sync, `kubectl set image`, rollout status) → tests against the live deployment (incl. smoke test) → promote `stable` tag on green / `kubectl rollout undo` + Teams alert on red. Nothing pulls `latest` at runtime — AKS pins the sha tag, so a half-validated image can't serve an incident.
**Impact:** sentinel ARCHITECTURE §9.2, workflow tables everywhere, sentinel-infra §3.1 image table, READMEs.

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

### 2026-07-02: Ephemeral backend — run inside GHA, not AKS *(SUPERSEDED 2026-07-04: AKS single replica — multi-job GHA design was unimplementable)*
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

### 2026-07-01: PR content generation endpoint — specialized agent for demo PRs *(SCOPE CHANGED 2026-07-05: rollback PR content only — demo PRs are template-based)*
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
