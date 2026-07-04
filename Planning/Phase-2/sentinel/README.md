# sentinel (this repo)

The sentinel repo IS the backend. It contains the multi-agent incident response pipeline, GHA orchestration workflows, CI/CD pipelines, and all planning docs.

## What Lives Here

- `src/sentinel/` — Backend code: agents, tools, memory, API, eval
- `.github/workflows/` — CI (quality gate), CD (build+push image), incident response pipeline
- `tests/` — Unit + integration tests
- `alembic/` — Database migrations (replaces Phase 1 `data/` directory)
- `Planning/` — Architecture docs, planning state

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Hosting | **Ephemeral** — Docker container inside GHA runner | No always-on cost. Start → validate → use → teardown per incident. |
| Database | Azure PostgreSQL B1MS + pgvector | Free 12 months, always-on. Vector similarity for memory. |
| LLM providers | **Anthropic (default) + OpenAI (fallback)** | Top models only. Sonnet for reasoning, Haiku for classification. Switch via env flag. |
| Agent patterns | Reflexion + Plan-Execute + Reverification | Self-correcting pipeline, not blind linear chain |
| HITL gate | Revert PR on sentinel-deployment | GitHub PR review IS the approval — no custom approval API |
| Decision branching | confidence ≥ 0.7 → rollback PR, else → escalate to human | Pipeline only acts when confident; otherwise dumps context to Teams |
| Backend ↔ GHA split | Backend reasons, GHA executes | Backend returns decision. GHA creates PRs, sends notifications, reports to Datadog. |
| Correlation | incident_id ↔ deploy_id ↔ PR ↔ Datadog ↔ LangFuse | Full end-to-end traceability |
| CI pipeline (PR) | ci_validation.yml | Fast gate — lint, typecheck, Docker build, run backend, health check, unit + integration tests |
| CI pipeline (merge) | ci_backend_validation.yml | Full validation — build+push to ACR, run from real image, all tests, smoke test, ACR cleanup |
| Incident pipeline | ci_incident_response.yml (repository_dispatch) | secrets → start backend → parallel fetch → agent pipeline → branch → notify → teardown → summary |
| Notification | Microsoft Teams (from GHA jobs) | Incoming webhook, not backend |
| PR content agent | `POST /generate/pr-content` | LLM-generates realistic PR titles + descriptions for demo PRs |
| Tracing | LangFuse cloud (tracing + prompts + scoring) | Free 50K observations/month |
| Event routing | Event Grid → Azure Functions → repository_dispatch | All Azure always-free tier |
| Terraform | 6 modules in sentinel-infra (no AKS) | Reproducible, reviewable, provider-agnostic |

## Database Tables

| Table | Purpose |
|-------|---------|
| `incidents` | Episodic memory — past incidents with embeddings |
| `services` | Semantic memory — service ownership, deps, runbooks |
| `revert_prs` | HITL audit trail — revert PR lifecycle (created → merged/closed) |
| `deployments` | Deploy ↔ incident ↔ PR ↔ Datadog correlation |

## Pipeline Decision Flow

```
Analysis confidence ≥ 0.7 AND root cause = specific deploy?
├── YES → Resolution agent prepares rollback spec
│         → GHA: generate PR content → create revert PR → notify Teams
│         → PR = HITL gate (reviewer merges or closes)
└── NO  → Escalate: Teams notification with full context, no PR created
Both paths → Judge scores trajectory → store to memory → teardown backend
```

## Incident Workflow Lifecycle

```
fetch-secrets → start-backend (docker run + /health + /ready)
→ parallel fetch (service info, PR details, Datadog logs)
→ run-agent-pipeline → branch (rollback PR / escalate)
→ notify Teams → teardown-backend (docker stop) → summary
```

## Workflows

| File | Name | Trigger | Purpose |
|------|------|---------|---------|
| `ci.yml` | Phase 1 CI | PR to main | Existing lint, format, typecheck, pytest |
| `ci_validation.yml` | `[sentinel] PR — validation` | PR to main (src/, tests/) | Fast gate — lint, typecheck, Docker build (local), run backend, health check, unit + integration tests |
| `ci_backend_validation.yml` | `[sentinel] backend — validation` | Push to main (src/, tests/) | Full post-merge — lint, typecheck, build+push image to ACR, run backend from real image, all tests, smoke test, ACR cleanup |
| `ci_incident_response.yml` | `[sentinel] incident response — full pipeline` | repository_dispatch | Real pipeline with full lifecycle |

## Key Docs

| Doc | Purpose |
|-----|---------|
| `Planning/Phase-2/sentinel/ARCHITECTURE.md` | Phase 2 architecture (ephemeral backend, PostgreSQL, agent pipeline, agentic loops, CI/CD, LangFuse) |
| `ARCHITECTURE.md` (root) | Phase 1 architecture (current codebase) |
| `TODO.md` (root) | Phase 1 task tracker |

## Status

Architecture finalized. All design questions resolved. Waiting on sentinel-infra to provision Azure resources before building.
