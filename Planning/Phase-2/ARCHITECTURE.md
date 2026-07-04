# Sentinel Phase 2 — System Architecture

> Autonomous DevOps incident response across three repos, zero always-on compute cost.
> Datadog detects failure → agent pipeline diagnoses → rollback PR or escalation → human reviews.

---

## 1. Three-Repo System

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              SENTINEL SYSTEM                                    │
│                                                                                 │
│  ┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐  │
│  │   sentinel           │  │  sentinel-deployment │  │   sentinel-infra     │  │
│  │   (backend + CI/CD)  │  │  (dummy app)         │  │   (Terraform IaC)    │  │
│  │                      │  │                      │  │                      │  │
│  │  Multi-agent         │  │  Flask API on        │  │  6 Terraform modules │  │
│  │  incident response   │  │  Azure App Service   │  │  that provision all  │  │
│  │  pipeline. Runs as   │  │  F1. Deployed via    │  │  Azure resources.    │  │
│  │  ephemeral Docker    │  │  GHA zip deploy.     │  │  OIDC auth, cross-   │  │
│  │  container inside    │  │  Intentional         │  │  repo secret         │  │
│  │  GHA runner per      │  │  failures generate   │  │  distribution.       │  │
│  │  incident.           │  │  real Datadog        │  │                      │  │
│  │                      │  │  signal.             │  │  terraform apply     │  │
│  │  Backend reasons.    │  │                      │  │  = entire stack.     │  │
│  │  GHA executes.       │  │  The target.         │  │                      │  │
│  └──────────────────────┘  └──────────────────────┘  └──────────────────────┘  │
│         │                          │                          │                  │
│         │ Workflows:               │ Workflow:                │ Workflows:       │
│         │ ci_validation            │ ci_app_deployment.yml    │ ci_infra_dry.yml │
│         │ ci_backend_validation    │                          │ ci_infra.yml     │
│         │ ci_incident_response     │                          │ ci_runners.yml   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

| Repo | Purpose | Runs On |
|------|---------|---------|
| **sentinel** | Agent pipeline, tools, memory, API, eval. The brain. | Ephemeral Docker container inside GHA runner |
| **sentinel-deployment** | Dummy FastAPI app. Deployed to Azure. Breaks on purpose. | Azure App Service F1 (always free) |
| **sentinel-infra** | Terraform modules. Provisions all Azure resources. | GHA + Terraform CLI |

---

## 2. End-to-End Flow

```
 sentinel-deployment                     Azure                        sentinel
┌────────────────────┐    ┌──────────────────────────────┐    ┌────────────────────────┐
│                    │    │                              │    │                        │
│  PR merged → GHA   │    │  App Service (F1)            │    │  ci_incident_response  │
│  deploys broken    │───►│  ← deploy lands here         │    │  .yml (GHA workflow)   │
│  code via zip      │    │                              │    │                        │
│                    │    │  Datadog monitors            │    │  7-job pipeline:       │
└────────────────────┘    │  App Service health/metrics  │    │                        │
                          │         │                     │    │  1. fetch-secrets      │
                          │         │ alert fires         │    │  2. start-backend      │
                          │         ▼                     │    │  3. fetch context      │
                          │  Event Grid → Function        │    │  4. run-agent-pipeline │
                          │  (bridge: repository_dispatch)│───►│  5. rollback/escalate  │
                          │                              │    │  6. teardown-backend   │
                          │  PostgreSQL (B1MS)            │    │  7. post-summary       │
                          │  ← agent reads/writes ───────│────│                        │
                          │                              │    │                        │
                          │  ACR (Standard)               │    │  docker run            │
                          │  ← image pulled from ────────│────│  sentinel-backend      │
                          │                              │    │  :latest               │
                          │  Key Vault                    │    │                        │
                          │  ← secrets fetched by GHA ───│────│  env vars injected     │
                          │                              │    │                        │
                          └──────────────────────────────┘    └──────────┬─────────────┘
                                                                        │
                                                              ┌─────────┴──────────┐
                                                              │                    │
                                                     confidence ≥ 0.7        confidence < 0.7
                                                              │                    │
                                                              ▼                    ▼
                                                     Create revert PR      Notify Teams
                                                     on sentinel-          with full context
                                                     deployment            (no PR, human
                                                              │            takes over)
                                                              │
                                                              ▼
                                                     PR = HITL gate
                                                     Reviewer merges
                                                     or closes
```

### Flow in Words

1. **sentinel-deployment** merges a PR (some intentionally broken) → GHA deploys to App Service
2. **Datadog** monitors App Service → detects failure → fires webhook
3. **Event Grid → Azure Function** bridges the alert to GitHub `repository_dispatch`
4. **sentinel's `ci_incident_response.yml`** starts:
   - Fetches secrets from Key Vault
   - Pulls and starts sentinel-backend Docker container from ACR
   - Validates `/health` + `/ready`
   - Fetches context in parallel (service info, PR details, Datadog logs)
   - Runs the agent pipeline via `POST /webhooks/incident`
   - Branches on the result: rollback PR or escalation
   - Notifies Teams, reports to Datadog, tears down the container

---

## 3. Agent Pipeline

Six agents, three agentic patterns (plan-execute, reflexion, reverification):

```
Webhook payload (enriched by GHA)
       │
       ▼
  ORCHESTRATOR (Sonnet)  ← plans before dispatching
       │
       ▼
  TRIAGE (Haiku)  ← classify severity, identify service, check duplicates
       │
       ▼
  ANALYSIS (Sonnet)  ← analyze logs + deploy data, form hypothesis
       │
       ▼
  REFLEXION (Haiku)  ← self-critique: confidence < 0.7? loop back (max 2×)
       │
       ├── confidence ≥ 0.7 + root cause = specific deploy
       │   │
       │   ▼
       │   RESOLUTION (Sonnet)  ← prepare rollback spec (target SHA + justification)
       │   │
       │   ▼
       │   REVERIFICATION (Haiku)  ← does fix match root cause? PASS/FAIL/ESCALATE
       │
       └── confidence < 0.7 or ambiguous root cause
           │
           ▼
           ESCALATE  ← return full context to GHA, no PR
       │
       ▼
  JUDGE (Haiku)  ← score trajectory quality (both paths)
       │
       ▼
  Store to PostgreSQL + complete LangFuse trace
```

### LLM Providers

| Role | Default (Anthropic) | Fallback (OpenAI) |
|------|-------------------|-------------------|
| Reasoning (orchestrator, analysis, resolution) | Sonnet 4.6 | gpt-4o |
| Classification (triage, reflexion, reverification, judge) | Haiku 4.5 | gpt-4o-mini |

Switch default provider: `SENTINEL_PRIMARY_PROVIDER=openai`. Automatic fallback on rate limit, timeout, or API error.

### Tools

| Tool | Purpose | Side Effects |
|------|---------|-------------|
| `get_service_metadata` | Look up service owner, deps, runbooks | Read-only (PostgreSQL) |
| `search_past_incidents` | Find similar past incidents | Read-only (PostgreSQL + pgvector) |
| `fetch_logs` | Get recent error logs | Read-only (Datadog API) |
| `get_deploy_details` | Get deploy history for a service | Read-only (PostgreSQL) |
| `prepare_rollback_spec` | Output target SHA + justification | No external calls — output only |
| `format_escalation` | Structure escalation context for Teams | No external calls — output only |

No tool touches external state. Backend reasons, GHA executes.

---

## 4. Azure Infrastructure

Provisioned by sentinel-infra via Terraform. 6 modules, all free tier.

```
sentinel-infra (terraform apply)
       │
       ├── modules/acr/           → Azure Container Registry (Standard, free 12 months)
       │                            Images: sentinel-backend, ci-runner
       │
       ├── modules/postgresql/    → PostgreSQL Flexible Server (B1MS, free 12 months)
       │                            DB: sentinel, extension: pgvector
       │                            Firewall: allow all (dev)
       │
       ├── modules/keyvault/      → Key Vault (always free)
       │                            9 secrets (API keys, DB password, webhooks)
       │                            RBAC: Terraform writes, GHA reads
       │
       ├── modules/event-grid/    → Event Grid Topic (always free)
       │                            Routes Datadog webhooks
       │
       ├── modules/functions/     → Azure Function (Consumption, always free)
       │                            Bridge: Event Grid → repository_dispatch
       │
       └── modules/app-service/   → App Service F1 (always free)
                                    Target for sentinel-deployment
```

### Authentication: OIDC Workload Identity Federation

No stored Azure secrets. GitHub proves identity via JWT.

```
GHA workflow → request JWT from GitHub OIDC provider
            → az login with JWT
            → Azure verifies issuer + subject (repo:org/name:ref)
            → grants scoped access token
```

One Azure AD app with federated credentials for all three repos. Terraform provisions the credentials.

### Cross-Repo Secret Distribution

Terraform auto-pushes secrets to GitHub repos via `github_actions_secret`:

```
terraform apply
    ├── sentinel repo secrets:     ACR_LOGIN_SERVER, ACR_USERNAME, ACR_PASSWORD,
    │                              AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID
    │
    └── sentinel-deployment repo:  AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID
```

Two layers: GitHub secrets = identity (who am I). Key Vault = runtime values (API keys, DB password).

---

## 5. Database

Azure PostgreSQL B1MS + pgvector. 4 tables:

| Table | Purpose |
|-------|---------|
| `incidents` | Episodic memory — past incidents with embeddings for similarity search |
| `services` | Semantic memory — service ownership, dependencies, runbooks |
| `revert_prs` | HITL audit trail — revert PR lifecycle (open → merged/closed) |
| `deployments` | Deploy ↔ incident ↔ PR ↔ Datadog correlation |

### Correlation

Every incident links to all related artifacts:

```
incident_id → alert_id (Datadog) + dd_event_id + correlation_id
            → deploy_id → pr_number + commit_sha + gha_run_id
            → langfuse_trace_id (LLM trace)
```

---

## 6. Observability

| Layer | Tool | What It Captures |
|-------|------|-----------------|
| Deploy events | Datadog | Every deploy attempt: stage, status, version, error |
| Agent traces | LangFuse | Every pipeline run: spans per agent, token usage, judge scores |
| Prompt management | LangFuse | System prompts loaded LangFuse-first, local fallback |
| Incident history | PostgreSQL | Full trajectory, root cause, resolution, MTTR |
| Notifications | Microsoft Teams | Rollback PR created / escalation needed / summary |

---

## 7. HITL Safety

The revert PR on sentinel-deployment IS the approval gate.

- Backend decides rollback vs escalate — never executes destructive actions
- GHA creates the PR — backend does not call GitHub API for PR creation
- Reviewer merges or closes — that's the human-in-the-loop
- No custom `/approvals` endpoint — GitHub's review system is the gate
- `revert_prs` table tracks audit trail (who approved, when, what happened)

---

## 8. Workflows (All Repos)

All workflow files use `ci_` prefix. Convention: `ci_<descriptive_scope>.yml`.

| File | Repo | Name | Trigger | Purpose |
|------|------|------|---------|---------|
| `ci_validation.yml` | sentinel | `[sentinel] PR — validation` | PR to main | Fast gate — lint, typecheck, Docker build (local), run backend, health check, unit + integration tests |
| `ci_backend_validation.yml` | sentinel | `[sentinel] backend — validation` | Push to main | Full post-merge — lint, typecheck, build+push image to ACR, run backend from real image, all tests, smoke test, ACR cleanup |
| `ci_incident_response.yml` | sentinel | `[sentinel] incident response — full pipeline` | repository_dispatch | Real incident pipeline with full lifecycle |
| `ci_app_deployment.yml` | sentinel-deployment | `[deployment] deploy — build and ship` | Push to main | Build → Deploy → Verify → Report to Datadog |
| `ci_infra_dry.yml` | sentinel-infra | `[infra] terraform — validate and plan` | Push/PR | Terraform validate + plan (dry run) |
| `ci_infra.yml` | sentinel-infra | `[infra] terraform — apply` | Push to main | Terraform apply |
| `ci_runners.yml` | sentinel-infra | `[infra] runners — build and push` | ci-images/ changes | Build + push CI runner images to ACR |

### Incident Pipeline Job Flow

```
ci_incident_response.yml (repository_dispatch)

  fetch-secrets          ← az keyvault secret show (OIDC)
       │
  start-backend          ← docker run from ACR + /health + /ready
       │
       ├────────────────────────────┐
       ▼                            ▼                          ▼
  fetch-service-info    fetch-pr-details             fetch-datadog-logs
       │                            │                          │
       └────────────┬───────────────┘──────────────────────────┘
                    ▼
  run-agent-pipeline     ← POST /webhooks/incident + poll
                    │
           ┌────────┴────────┐
           │                 │
     rollback path      escalate path
           │                 │
  generate-pr-content   notify-escalation
           │                 │
  create-rollback-pr         │
           │                 │
  notify-rollback            │
           │                 │
           └────────┬────────┘
                    ▼
  teardown-backend   ← docker stop + rm
                    ▼
  post-summary       ← Datadog event + Teams summary
```

---

## 9. Cost

| Resource | Monthly Cost |
|----------|-------------|
| PostgreSQL B1MS | Free (12-month) |
| ACR Standard | Free (12-month) |
| Key Vault | Free (always) |
| Event Grid | Free (always) |
| Azure Functions | Free (always) |
| App Service F1 | Free (always) |
| TF state storage | ~$0.01 |
| LLM calls (Anthropic + OpenAI) | ~$5-12 |
| GHA minutes | Free (2,000/month) |
| LangFuse | Free (50K obs/month) |
| **Total** | **~$5-12/month** |

No compute costs for backend — runs ephemerally inside GHA runner.

---

## 10. Detailed Architecture Docs

| Doc | Scope |
|-----|-------|
| [sentinel/ARCHITECTURE.md](sentinel/ARCHITECTURE.md) | Agent pipeline, API contracts, agentic loops, reflexion, tools, models, workflows, DB schema, LangFuse, Dockerfile, Phase 1 cleanup |
| [sentinel-deployment/ARCHITECTURE.md](sentinel-deployment/ARCHITECTURE.md) | Deploy pipeline stages, Datadog event schema, demo PR sequence, app structure |
| [sentinel-infra/ARCHITECTURE.md](sentinel-infra/ARCHITECTURE.md) | 6 Terraform modules (HCL), OIDC setup, cross-repo secrets, Key Vault policies, PostgreSQL firewall, CI/CD workflows, bootstrap checklist |
| [STATE.md](STATE.md) | Planning state, decision log, blockers |
