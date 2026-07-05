# Sentinel Phase 2 — System Architecture

> Autonomous DevOps incident response across three repos. Backend on-demand on AKS — single replica, scale-to-zero between runs.
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
│  │  Multi-agent         │  │  FastAPI app on      │  │  7 Terraform modules │  │
│  │  incident response   │  │  Azure App Service   │  │  that provision all  │  │
│  │  pipeline. Runs as   │  │  F1. Deployed via    │  │  Azure resources     │  │
│  │  single-replica      │  │  GHA zip deploy.     │  │  incl. AKS. OIDC     │  │
│  │  Deployment on AKS   │  │  Intentional         │  │  auth, cross-repo    │  │
│  │  (1 free node).      │  │  failures generate   │  │  secret              │  │
│  │  Scale-to-zero.      │  │  real Datadog        │  │  distribution.       │  │
│  │                      │  │  signal.             │  │  terraform apply     │  │
│  │  Backend reasons.    │  │                      │  │  = entire stack.     │  │
│  │  GHA executes.       │  │  The target.         │  │                      │  │
│  └──────────────────────┘  └──────────────────────┘  └──────────────────────┘  │
│         │                          │                          │                  │
│         │ Workflows:               │ Workflows:               │ Workflows:       │
│         │ ci_validation            │ ci_app_deployment.yml    │ ci_infra_dry.yml │
│         │ ci_backend_deployment    │ ci_demo_prs.yml          │ ci_infra.yml     │
│         │ ci_incident_response     │                          │ ci_runners.yml   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

| Repo | Purpose | Runs On |
|------|---------|---------|
| **sentinel** | Agent pipeline, tools, memory, API, eval. The brain. | AKS — single-replica Deployment, node pool scaled 0↔1 per run (free control plane + 1× B2ats_v2 node) |
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
│  code via zip +    │    │                              │    │                        │
│  records deploy    │    │  Datadog monitors            │    │  6-stage pipeline:     │
│  row in PostgreSQL │    │  App Service health/metrics  │    │                        │
└────────────────────┘    │         │                     │    │  1. scale up backend   │
                          │         │ alert fires         │    │  2. fetch context ∥    │
                          │         ▼                     │    │  3. run-agent-pipeline │
                          │  Event Grid → Function        │    │     POST → AKS backend │
                          │  (bridge: repository_dispatch)│───►│  4. rollback/escalate  │
                          │                              │    │  5. scale to zero      │
                          │  AKS (node pool 0↔1 per run)  │    │  6. post-summary       │
                          │  ← sentinel-backend lives     │◄───│  calls backend at      │
                          │    here (1 replica; node      │    │  per-run LB URL (from  │
                          │    idles at 0 between runs)   │    │  cluster) + token      │
                          │                              │    │                        │
                          │  PostgreSQL (B1MS)            │    │                        │
                          │  ← backend reads/writes ─────│────│                        │
                          │                              │    │                        │
                          │  ACR (Standard)               │    │                        │
                          │  ← images pushed by CI,       │    │                        │
                          │    pulled by AKS              │    │                        │
                          │                              │    │                        │
                          │  Key Vault                    │    │                        │
                          │  ← secrets read per-job by    │    │                        │
                          │    GHA + synced to K8s Secret │    │                        │
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
                                                     Reviewer merges or
                                                     closes (outcome not
                                                     tracked — see §7)
```

### Flow in Words

1. **sentinel-deployment** merges a PR (some intentionally broken) → GHA deploys to App Service and records the deploy row in PostgreSQL (success or failure)
2. **Datadog** detects the failure via one of two monitors — deploy-failure events (condition C: pipeline failed) or runtime health (condition B: deploy was green but the site broke) — and fires the webhook
3. **Event Grid → Azure Function** bridges the alert to GitHub `repository_dispatch`
4. **sentinel's `ci_incident_response.yml`** starts (scale-to-zero: the backend idles at 0 and is scaled up per run):
   - Scales the backend up (node pool 0→1, replicas 0→1, wait `/ready` — ~3-7 min cold, instant in a `KEEP_WARM` session) and resolves the backend URL from the cluster (no static URL variable — it flows to later jobs as a job output); if it can't come up → Teams alert + loud workflow failure, the incident is never silently dropped
   - Fetches context in parallel (service info, PR details, Datadog logs) — each job reads its own secrets from Key Vault (GHA can't pass secrets between jobs)
   - Runs the agent pipeline via `POST /webhooks/incident` on the AKS backend and polls for the result (10-min cap → escalation on timeout)
   - Branches on the result: rollback PR (+ writes `pr_number`/`pr_url` on the incident row) or escalation
   - Scales the backend back to zero (`if: always()`, skipped when `SENTINEL_KEEP_WARM=true`)
   - Notifies Teams, reports to Datadog

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

Provisioned by sentinel-infra via Terraform. 7 modules, all free tier.

```
sentinel-infra (terraform apply)
       │
       ├── modules/aks/           → AKS cluster (control plane always free,
       │                            1× B2ats_v2 node free 12 months)
       │                            Hosts sentinel-backend (single replica, public LB)
       │                            Kubelet gets AcrPull; app deployed by sentinel CI
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

Azure PostgreSQL B1MS + pgvector. 3 tables:

| Table | Purpose |
|-------|---------|
| `incidents` | Episodic memory — past incidents with embeddings for similarity search. Carries `pr_number`/`pr_url` when a revert PR was created (creation record only) |
| `services` | Semantic memory — service ownership, dependencies, runbooks |
| `deployments` | Deploy ↔ incident ↔ PR ↔ Datadog correlation — written by `ci_app_deployment.yml` on every deploy (success and failure) |

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

The revert PR on sentinel-deployment IS the approval gate. **Fire-and-forget.**

- Backend decides rollback vs escalate — never executes destructive actions
- GHA creates the PR — backend does not call GitHub API for PR creation
- Reviewer merges or closes — that's the human-in-the-loop
- No custom `/approvals` endpoint — GitHub's review system is the gate
- Sentinel's job ends when the PR exists and Teams is notified. It does NOT watch,
  poll, or record the PR outcome — no waiting states anywhere. The incident row
  stores `pr_number`/`pr_url` as a creation record; the PR on GitHub is the audit
  trail. MTTR = pipeline duration (alert → decision), not human review latency.

---

## 8. Workflows (All Repos)

All workflow files use `ci_` prefix. Convention: `ci_<descriptive_scope>.yml`.

Repeated step blocks live as **reusable composite actions** in sentinel's
`.github/actions/` — `backend-up` (outputs the per-run backend URL), `backend-down`,
`get-kv-secrets`, `notify-teams`, `psql-exec` — plus a local `dd-report` action in
sentinel-deployment. sentinel-deployment reuses sentinel's actions cross-repo
(`uses: <owner>/sentinel/.github/actions/psql-exec@main`).

| File | Repo | Name | Trigger | Purpose |
|------|------|------|---------|---------|
| `ci_validation.yml` | sentinel | `[sentinel] PR — validation` | PR to main | Fast gate — quality checks + single-job build/run/test (no ACR, no AKS, stubbed LLM) |
| `ci_backend_deployment.yml` | sentinel | `[sentinel] backend — deployment` | Push to main | Build+push to ACR (sha tag), scale up AKS, deploy, validate rollout, all tests against live deployment, smoke test, promote or `rollout undo`, scale to zero |
| `ci_incident_response.yml` | sentinel | `[sentinel] incident response — full pipeline` | repository_dispatch | Real incident pipeline — scales the backend up, runs, scales it down |
| `ci_backend_scale.yml` | sentinel | `[sentinel] backend — scale` | workflow_dispatch + nightly cron | Manual up/down toggle + auto-down safety net for scale-to-zero |
| `ci_app_deployment.yml` | sentinel-deployment | `[deployment] deploy — build and ship` | Push to main | Build → Deploy → Verify → Record (PostgreSQL) → Report to Datadog |
| `ci_demo_prs.yml` | sentinel-deployment | `[deployment] demo — create scenario PRs` | workflow_dispatch | Open intentional-failure PRs from static scenario templates — no backend involvement |
| `ci_infra_dry.yml` | sentinel-infra | `[infra] terraform — validate and plan` | Push/PR | Terraform validate + plan (dry run) |
| `ci_infra.yml` | sentinel-infra | `[infra] terraform — apply` | Push to main | Terraform apply |
| `ci_runners.yml` | sentinel-infra | `[infra] runners — build and push` | ci-images/ changes | Build + push CI runner images to ACR |

### Incident Pipeline Job Flow

```
ci_incident_response.yml (repository_dispatch) — scale-to-zero, serialized via
the `sentinel-backend` concurrency group

  ensure-backend-up  ← node pool 0→1, replicas 0→1, wait /ready (~3-7 min cold)
       │                → resolves + outputs backend-url (no static URL variable)
       │                can't come up → Teams alert + fail loudly
       ├────────────────────────────┬──────────────────────────┐
       ▼                            ▼                          ▼
  fetch-service-info    fetch-pr-details             fetch-datadog-logs
       │    (each job reads its own secrets from Key Vault — OIDC;
       │     GHA blocks secrets in job outputs)
       └────────────┬───────────────┴──────────────────────────┘
                    ▼
  run-agent-pipeline     ← POST ${BACKEND_URL}/webhooks/incident + poll (10-min cap,
                    │      timeout degrades to escalation)
           ┌────────┴────────┐
           │                 │
     rollback path      escalate path
           │                 │
  generate-pr-content   notify-escalation
           │                 │
  create-rollback-pr         │
  (+ psql UPDATE incidents   │
   SET pr_number, pr_url)    │
           │                 │
  notify-rollback            │
           │                 │
           └────────┬────────┘
                    ▼
  teardown-backend   ← scale to zero (if: always(); skipped when KEEP_WARM)
                    ▼
  post-summary       ← Datadog event + Teams summary
```

---

## 9. Cost

| Resource | Monthly Cost |
|----------|-------------|
| AKS control plane | Free (always) |
| AKS node (1× B2ats_v2) | Free — scale-to-zero uses ~20-80 of the 750 free hrs/mo |
| LoadBalancer public IP | ~$0 — released at teardown; URL resolved fresh per run |
| PostgreSQL B1MS | Free (12-month) |
| ACR Standard | Free (12-month) |
| Key Vault | Free (always) |
| Event Grid | Free (always) |
| Azure Functions | Free (always) |
| App Service F1 | Free (always) |
| TF state storage | ~$0.01 |
| LLM calls (Anthropic + OpenAI) | ~$5-12 |
| GHA minutes | Free (GitHub Pro — 3,000/month) |
| LangFuse | Free (50K obs/month) |
| **Total** | **~$5-12/month** |

Backend compute = the free AKS node, scaled to zero between runs — the unused
~670 free B2ats_v2 hours/month stay available for other projects. After 05/2027 the
node bills only for scaled-up hours (a few $/month) — `terraform destroy` when the
project wraps.

---

## 10. Detailed Architecture Docs

| Doc | Scope |
|-----|-------|
| [sentinel/ARCHITECTURE.md](sentinel/ARCHITECTURE.md) | Agent pipeline, API contracts, agentic loops, reflexion, tools, models, workflows, DB schema, LangFuse, Dockerfile, Phase 1 cleanup |
| [sentinel-deployment/ARCHITECTURE.md](sentinel-deployment/ARCHITECTURE.md) | Deploy pipeline stages, Datadog event schema, demo PR sequence, app structure |
| [sentinel-infra/ARCHITECTURE.md](sentinel-infra/ARCHITECTURE.md) | 6 Terraform modules (HCL), OIDC setup, cross-repo secrets, Key Vault policies, PostgreSQL firewall, CI/CD workflows, bootstrap checklist |
| [STATE.md](STATE.md) | Planning state, decision log, blockers |
