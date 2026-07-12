# sentinel (this repo)

The sentinel repo IS the backend. It contains the multi-agent incident response pipeline, GHA orchestration workflows, CI/CD pipelines, and all planning docs.

## What Lives Here

- `src/sentinel/` — Backend code: agents, tools, memory, API, eval, providers — all reasoning
- `azure/k8s/` — Deployment + ServiceAccount (workload identity) + Service manifests (Service created/deleted per run — dynamic URL)
- `.github/workflows/` — CI (PR gate), deploy-to-AKS, incident response, scale toggle
- `.github/actions/` — Reusable composite actions: `backend-up`/`backend-down`, `get-kv-secrets`, `notify-teams`, `psql-exec`
- `tests/` — Unit + integration tests
- `alembic/` — Database migrations (replaces Phase 1 `data/` directory)
- `Planning/` — Architecture docs, planning state

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Hosting | **AKS scale-to-zero** — single-replica Deployment, node pool 0↔1 per run | Backend URL resolved from the cluster each run (`backend-up` output — no static variable). Service + LB IP deleted at teardown → **$0 infra at idle**. ~20-80 of 750 free node-hrs/mo; `SENTINEL_KEEP_WARM=true` for demo sessions. |
| Database | Azure PostgreSQL B1MS + pgvector | Free 12 months, always-on. Vector similarity for memory. |
| LLM providers | **Anthropic (default) + OpenAI (fallback)** | Top models only. Sonnet for reasoning, Haiku for classification. Switch via env flag. |
| Agent patterns | Reflexion + Plan-Execute + Reverification | Self-correcting pipeline, not blind linear chain |
| HITL gate | Revert PR on sentinel-deployment — **fire-and-forget** | GitHub PR review IS the approval. Sentinel records PR creation (`pr_number`/`pr_url` on the incident) but never tracks merge/close outcome. |
| Decision branching | confidence ≥ 0.7 → rollback PR, else → escalate to human | Pipeline only acts when confident; otherwise dumps context to Teams |
| Backend ↔ GHA split | Backend reasons, GHA executes | Backend returns decision. GHA creates PRs, sends notifications, reports to Datadog. |
| Correlation | incident_id ↔ deploy_id ↔ PR ↔ Datadog ↔ LangFuse | Full end-to-end traceability |
| CI pipeline (PR) | ci_validation.yml | Fast gate — lint, typecheck, Docker build, run backend, health check, unit + integration tests |
| CI pipeline (merge) | ci_backend_deployment.yml | Build+push to ACR (sha tag), scale up AKS, deploy, validate rollout, tests against live deployment, promote or `rollout undo`, scale to zero |
| Incident pipeline | ci_incident_response.yml (repository_dispatch) | ensure-backend-up → parallel fetch → agent pipeline → branch → teardown-backend → summary; serialized via `sentinel-backend` concurrency group |
| Notification | Microsoft Teams (from GHA jobs) | Incoming webhook, not backend |
| PR content agent | `POST /generate/pr-content` | Rollback PR title + description only — sole consumer is ci_incident_response. sentinel-deployment scenarios are real git branches (no backend). |
| Inbound API auth | **Entra bearer** (`api://sentinel-backend` + `Incident.Write`), validated vs JWKS | Deletes the shared `sentinel-api-token`; `/health`+`/ready` stay open |
| Backend → Azure auth | **AKS workload identity** (UAMI) | Pod reads Key Vault + gets a PostgreSQL Entra token with no stored secret (no K8s Secret sync) |
| Database auth | **Entra-only** (short-lived token as password) | No `db-password` anywhere |
| Two-case handling | `signal_type`: `deploy_failure` → rollback fast path, `runtime_error` → full pipeline | Stamped by the Event Grid bridge from the 30 scenario branches |
| Eval dataset | 30 sentinel-deployment scenario branches (`branches.yaml`) | Replaces Phase-1 synthetic scenario JSON |
| Tracing | LangFuse cloud (tracing + prompts + scoring) | Free 50K observations/month |
| Event routing | Event Grid → Azure Functions → repository_dispatch (bridge stamps `signal_type`) | All Azure always-free tier |
| Terraform | 7 modules + identity plane in sentinel-infra (incl. AKS) | Reproducible, reviewable, provider-agnostic |

## Database Tables

| Table | Purpose |
|-------|---------|
| `incidents` | Episodic memory — past incidents with embeddings; carries `pr_number`/`pr_url` when a revert PR was created |
| `services` | Semantic memory — service ownership, deps, runbooks |
| `deployments` | Deploy ↔ incident ↔ PR ↔ Datadog correlation — written by `ci_app_deployment.yml` on every deploy |

## Pipeline Decision Flow

```
Analysis confidence ≥ 0.7 AND root cause = specific deploy?
├── YES → Resolution agent prepares rollback spec
│         → GHA: generate PR content → create revert PR → notify Teams
│         → PR = HITL gate (reviewer merges or closes)
└── NO  → Escalate: Teams notification with full context, no PR created
Both paths → Judge scores trajectory → store to memory (terminal state at pipeline end)
```

## Incident Workflow Lifecycle

```
ensure-backend-up (scale-to-zero: node pool 0→1, replicas 0→1, wait /ready,
                   resolve backend-url from cluster — ~3-7 min cold;
                   can't come up → Teams alert + loud failure)
→ parallel fetch (service info, PR details, Datadog logs — per-job Key Vault reads)
→ run-agent-pipeline (POST to AKS backend + poll, 10-min cap)
→ branch (rollback PR + record pr_number/pr_url / escalate)
→ teardown-backend (scale to zero — skipped when SENTINEL_KEEP_WARM=true)
→ notify Teams → summary
```

## Workflows

| File | Name | Trigger | Purpose |
|------|------|---------|---------|
| `ci.yml` | Phase 1 CI | PR to main | Existing lint, format, typecheck, pytest |
| `ci_validation.yml` | `[sentinel] PR — validation` | PR to main (src/, tests/) | Fast gate — quality checks + single-job build/run/test (no ACR, no AKS, stubbed LLM) |
| `ci_backend_deployment.yml` | `[sentinel] backend — deployment` | Push to main (src/, tests/, azure/) | Build+push to ACR, scale up AKS, deploy, validate, tests against live deployment, promote or rollback, scale to zero |
| `ci_incident_response.yml` | `[sentinel] incident response — full pipeline` | repository_dispatch | Real pipeline — scales backend up, runs, scales down |
| `ci_backend_scale.yml` | `[sentinel] backend — scale` | workflow_dispatch + nightly cron | Manual up/down toggle + auto-down safety net (scale-to-zero) |

## Key Docs

| Doc | Purpose |
|-----|---------|
| `Planning/Phase-2/sentinel/ARCHITECTURE.md` | Phase 2 architecture (AKS backend, PostgreSQL, agent pipeline, agentic loops, CI/CD, LangFuse) |
| `ARCHITECTURE.md` (root) | Phase 1 architecture (current codebase) |
| `TODO.md` (root) | Phase 1 task tracker |

## Status

Architecture finalized (**rev 4 — 2026-07-12**): inbound Entra bearer auth (§3.6),
workload identity for Key Vault + PostgreSQL (no K8s Secret), `signal_type` two-case
handling, and eval driven by the 30 sentinel-deployment scenario branches. Prior rev-3
model intact (AKS scale-to-zero, `ci_backend_scale.yml`, `/generate/pr-content` rollback
only). Waiting on sentinel-infra to provision Azure resources before building.
