# Sentinel Phase 2 — Architecture Index

> **The high-level map.** Sentinel is an autonomous DevOps incident-response agent across
> three repos: Datadog detects a failure → a multi-agent pipeline diagnoses it → a rollback
> PR or an escalation → a human reviews. This file is the *whole picture* — system diagrams
> plus a one-paragraph summary of every concern, each pointing to the file that specifies it
> in depth.
>
> **Agents & skills:** read this for context, then follow the **`→ deep dive`** pointer to the
> authoritative file when implementing a task. This index does **not** restate detail — it
> routes you to it.

## How to navigate

| You want… | Go to |
|-----------|-------|
| The whole picture, fast | §1–§2 diagrams + §3 summaries (this file) |
| Which file specifies concern X | **§4 Architecture Map** (this file) |
| Deep detail to implement a task | the per-repo file your task's **Arch refs** cite (§4) |
| Build order, tasks, status | [Phase-2-Implementation/](../Phase-2-Implementation/README.md) — tracker · 58 tasks · 16 phases |
| Decisions, blockers, history | [STATE.md](STATE.md) |

**Deep-dive files (authoritative for detail):**
[sentinel (backend)](sentinel/ARCHITECTURE.md) · [sentinel-deployment](sentinel-deployment/ARCHITECTURE.md) · [sentinel-infra](sentinel-infra/ARCHITECTURE.md).
Per-task **Arch refs** point into these at the section level (e.g. `sentinel §3.6`).

---

## 1. Three-Repo System

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              SENTINEL SYSTEM                                      │
│                                                                                  │
│  ┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐   │
│  │   sentinel           │  │  sentinel-deployment │  │   sentinel-infra     │   │
│  │   (backend + CI/CD)  │  │  (the target app)    │  │   (Terraform IaC)    │   │
│  │                      │  │                      │  │                      │   │
│  │  Multi-agent         │  │  FastAPI app on      │  │  7 Terraform modules │   │
│  │  incident-response   │  │  App Service F1.     │  │  + identity plane.   │   │
│  │  pipeline on AKS,    │  │  30 scenario         │  │  Provisions the      │   │
│  │  scale-to-zero.      │  │  branches generate   │  │  whole Azure stack.  │   │
│  │                      │  │  real Datadog        │  │  terraform apply     │   │
│  │  Backend reasons.    │  │  signal. Ground      │  │  = everything;       │   │
│  │  GHA executes.       │  │  truth + eval set.   │  │  ci_destroy = gone.  │   │
│  └──────────────────────┘  └──────────────────────┘  └──────────────────────┘   │
│         │                          │                          │                  │
│         │ Workflows:               │ Workflow:                │ Workflows:       │
│         │ ci_validation            │ ci_app_deployment.yml    │ ci_infra_dry     │
│         │ ci_backend_deployment    │ (deploys 1 of 30         │ ci_infra         │
│         │ ci_incident_response     │  scenario branches)      │ ci_destroy_infra │
│         │ ci_backend_scale         │                          │ ci_runners       │
└─────────────────────────────────────────────────────────────────────────────────┘
```

| Repo | Purpose | Runs on | → deep dive |
|------|---------|---------|-------------|
| **sentinel** | Agent pipeline, tools, memory, API, eval. The brain. | AKS — single-replica Deployment, node pool scaled 0↔1 per run | [sentinel/ARCHITECTURE.md](sentinel/ARCHITECTURE.md) |
| **sentinel-deployment** | Target app + deploy pipeline. Breaks on purpose to make signal. | Azure App Service F1 (always free) | [sentinel-deployment/ARCHITECTURE.md](sentinel-deployment/ARCHITECTURE.md) |
| **sentinel-infra** | Terraform modules + identity plane. Provisions all Azure resources. | GHA + Terraform CLI | [sentinel-infra/ARCHITECTURE.md](sentinel-infra/ARCHITECTURE.md) |

**Implementation order:** infra → deployment → backend (each phase = 1 branch + 1 PR, merged
after a human phase gate). See the [tracker](../Phase-2-Implementation/README.md).

---

## 2. End-to-End Incident Flow

```
 sentinel-deployment                     Azure                        sentinel
┌────────────────────┐    ┌──────────────────────────────┐    ┌────────────────────────┐
│  Deploy a scenario │    │  App Service (F1)            │    │  ci_incident_response  │
│  branch → GHA      │───►│  ← deploy lands here         │    │  .yml (GHA workflow)   │
│  deploys + records │    │                              │    │                        │
│  deploy row in PG  │    │  Datadog monitors            │    │  scale-to-zero pipeline│
│  (Entra DB token)  │    │  (deploy-failure / runtime)  │    │  1. scale up backend   │
└────────────────────┘    │         │ alert fires        │    │  2. fetch context ∥    │
                          │         ▼                    │    │  3. run agent pipeline │
                          │  Event Grid → Function        │    │  4. rollback/escalate  │
                          │  bridge: stamps signal_type   │───►│  5. scale to zero      │
                          │  → repository_dispatch        │    │  6. post-summary       │
                          │                              │    │                        │
                          │  AKS (node 0↔1 per run)       │◄───│  POST /webhooks/incident│
                          │  ← sentinel-backend lives     │    │  at per-run LB URL +   │
                          │    here (idles at 0)          │    │  Entra bearer token    │
                          │                              │    │                        │
                          │  PostgreSQL ← backend RW      │    │                        │
                          │  ACR ← images  ·  Key Vault   │    │                        │
                          │  Backend auth = workload      │    │                        │
                          │  identity (no pod secret)     │    │                        │
                          └──────────────────────────────┘    └──────────┬─────────────┘
                                                                        │
                                                     confidence ≥ 0.7 ──┴── confidence < 0.7
                                                              │                    │
                                                              ▼                    ▼
                                                     Revert PR on          Notify Teams
                                                     sentinel-deployment   (no PR — human
                                                     = HITL gate           takes over)
```

1. **sentinel-deployment** deploys one of its **30 scenario branches** → records the deploy row in PostgreSQL (success or failure). `→ deep dive: sentinel-deployment §3–§4`
2. **Datadog** fires one of two monitors — `deploy_failure` (case ii, previous version stays live) or `runtime_error` (case iii, green deploy but the app breaks). Case i (clean) fires neither.
3. **Event Grid → Function** bridges the alert to `repository_dispatch`, **stamping `signal_type`** so the backend branches: deploy_failure → rollback fast path, runtime_error → full pipeline. `→ deep dive: sentinel-infra §3.4–§3.5`
4. **`ci_incident_response.yml`** scales the backend up, fetches context in parallel, POSTs to the backend (Entra bearer), branches on the result (rollback PR or escalation), scales back to zero, notifies Teams. `→ deep dive: sentinel §9.3`

---

## 3. Cross-Cutting Architecture — at a glance

### 3.1 Agent pipeline — the brain
Six agents, three agentic patterns (plan-execute · reflexion · reverification). The
orchestrator branches on `signal_type`; the runtime path self-corrects via a reflexion loop
and gates action on confidence.

```
Webhook (signal_type, GHA-enriched)
  → ORCHESTRATOR (plans; branches on signal_type)
        deploy_failure → rollback fast path
        runtime_error  → TRIAGE → ANALYSIS → REFLEXION (loop if conf<0.7, ≤2×)
  → conf ≥0.7 + specific deploy → RESOLUTION (rollback spec) → REVERIFICATION
  → else → ESCALATE
  → JUDGE (scores both paths) → store PostgreSQL + LangFuse trace
```
Tools are read-only or output-only — **no tool touches external state** (backend reasons, GHA
executes). Models: Sonnet for reasoning, Haiku for classification; Anthropic default, OpenAI
fallback. **`→ deep dive: sentinel §4` (loops, models, tools) · `§3.1` (two-case handling)**

### 3.2 Identity & secrets — one identity, many audience-scoped tokens
No stored cloud client secret, no shared API token, no DB password. Three trust edges, each a
short-lived Entra token stamped for one audience:
- **GHA → Azure:** OIDC federation (GitHub JWT → ARM token). No `AZURE_CLIENT_SECRET`.
- **Caller → backend API:** Entra bearer (`api://sentinel-backend` + `Incident.Write`), validated vs JWKS. No `sentinel-api-token`.
- **Backend pod → Azure:** AKS workload identity → tokens for Key Vault + PostgreSQL. No pod secret.

PostgreSQL is **Entra-only** (token as password); LLM keys sit on a **Key Vault rotation
policy**. Only `GITHUB_PAT` + `ACR_*` remain as bootstrap secrets. **`→ deep dive: sentinel-infra
§4` (OIDC + Entra + token model) · `§3.2` (Entra DB) · `§3.3/§3.8` (Key Vault + rotation) · `§3.7`
(workload identity) · `sentinel §3.6` (inbound validation)**

### 3.3 Ground truth & signal types — the 30 scenario branches
The deploy app is **real ground truth**: 30 pre-authored branches (10 per case) that also serve
as the eval dataset. Two signal types drive two backend paths; case i is a no-op.

| Case | Branches | What happens | Signal → backend path |
|------|----------|--------------|-----------------------|
| i — clean pass | `pass/01..10` | green deploy, healthy | none (no monitor) |
| ii — deploy fails | `deployfail/01..10` | pipeline red, previous version stays live | `deploy_failure` → rollback |
| iii — runtime error | `runtime/01..10` | green deploy, live app breaks | `runtime_error` → full incident response |

**`→ deep dive: sentinel-deployment §4` (cases + `branches.yaml` catalog)**

### 3.4 Data & correlation
Azure PostgreSQL B1MS + pgvector, 3 tables: **`incidents`** (episodic memory + embeddings),
**`services`** (semantic memory), **`deployments`** (deploy↔incident↔PR↔Datadog). Every incident
threads a correlation chain `incident_id → alert/dd_event/correlation_id → deploy_id → pr/sha →
langfuse_trace_id`. **`→ deep dive: sentinel §5`**

### 3.5 Observability
Datadog (deploy events + logs = the signal), LangFuse (agent traces, prompt management, judge
scores), PostgreSQL (incident history + MTTR), Microsoft Teams (notifications). **`→ deep dive:
sentinel §6` (LangFuse) · `sentinel-deployment §6` (Datadog schema)**

### 3.6 HITL safety — fire-and-forget
The only destructive action is a revert PR, and **the revert PR on sentinel-deployment IS the
approval gate**. Backend decides rollback vs escalate but never acts on the world; GHA opens the
PR; a human merges or closes. No `/approvals` endpoint, no PR-outcome tracking. **`→ deep dive:
sentinel §3.3 + §7`**

### 3.7 Workflows
`ci_`-prefixed; repeated step blocks are **reusable composite actions** in sentinel's
`.github/actions/` (reused cross-repo).

| File | Repo | Purpose |
|------|------|---------|
| `ci_validation` | sentinel | Fast PR gate (quality + build/run/test, stubbed LLM) |
| `ci_backend_deployment` | sentinel | Build+push → deploy to AKS → test live → promote/rollback → scale to zero |
| `ci_incident_response` | sentinel | Real pipeline (repository_dispatch) — scale up, run, scale down |
| `ci_backend_scale` | sentinel | Manual up/down + nightly auto-down |
| `ci_app_deployment` | sentinel-deployment | Build → Deploy → Verify → Record → Datadog (one scenario branch) |
| `ci_infra_dry` / `ci_infra` | sentinel-infra | Terraform validate+plan / apply |
| `ci_destroy_infra` | sentinel-infra | Manual full teardown (destroy + `az group delete`) |
| `ci_runners` | sentinel-infra | Build + push CI runner images |

**`→ deep dive: sentinel §9` (job flows + composite actions) · `sentinel-infra §7` (infra CI + destroy) · `sentinel-deployment §3` (deploy pipeline)**

### 3.8 Backend hosting — AKS scale-to-zero
Single-replica Deployment; node pool idles at **0** and scales 0↔1 per run. No static backend
URL — resolved from the cluster each run; the LB Service (and public IP) is deleted at teardown
→ **$0 infra at idle**. `KEEP_WARM` keeps it up for live demos. **`→ deep dive: sentinel §8`
(manifests, workload identity) · `§8.5` (scale-to-zero lifecycle)**

---

## 4. Architecture Map — where each thing is specified

Fast lookup: a concern → the authoritative file + section. (Your task's **Arch refs** already
name the section; this is the reverse index.)

| Concern | Authoritative spec |
|---------|--------------------|
| Agent pipeline, loops, models | sentinel §4 |
| Tools (contracts, side effects) | sentinel §4.8, §13.2 |
| API contracts + `signal_type` handling | sentinel §3.1–§3.4 |
| Inbound Entra bearer validation | sentinel §3.6 |
| DB schema + correlation | sentinel §5 |
| LangFuse tracing / prompts / scoring | sentinel §6 |
| HITL / revert-PR gate | sentinel §3.3, §7 |
| Docker + K8s manifests + workload identity | sentinel §7, §8 |
| Scale-to-zero lifecycle | sentinel §8.5 |
| sentinel CI/CD (validation, deploy, incident, scale) | sentinel §9 |
| Phase-1 → Phase-2 migration cleanup | sentinel §13 |
| Terraform modules (ACR/PG/KV/EventGrid/Functions/AppService/AKS) | sentinel-infra §3 |
| Entra-only PostgreSQL auth | sentinel-infra §3.2 |
| Key Vault RBAC + rotation Function | sentinel-infra §3.3, §3.8 |
| AKS workload identity + backend UAMI | sentinel-infra §3.7 |
| Event Grid two-signal routing + bridge | sentinel-infra §3.4–§3.5 |
| OIDC + Entra identity plane, backend app reg, token model | sentinel-infra §4 |
| Cross-repo secret/variable distribution | sentinel-infra §5 |
| Infra CI/CD + `ci_destroy_infra` | sentinel-infra §7 |
| Bootstrap checklist | sentinel-infra §10 |
| The app + deploy pipeline + Datadog schema | sentinel-deployment §2–§3, §6 |
| 30 scenario branches (3 cases) + catalog | sentinel-deployment §4 |
| Datadog monitors | sentinel-deployment §6.3 |
| Cost breakdown | sentinel-infra §11 (see §5 below for the roll-up) |
| Decisions, blockers, history | [STATE.md](STATE.md) |

---

## 5. Cost (roll-up)

**~$5–12/month — LLM calls only; $0 infra at idle.** Everything else sits in Azure free /
12-month-free tiers (AKS control plane, PostgreSQL B1MS, ACR, Key Vault, Event Grid, Functions,
App Service F1) plus GitHub Pro (3,000 GHA min/mo), Datadog Student Pro, and LangFuse free. The
AKS node scales to zero between runs (~20–80 of 750 free hrs/mo). **`→ deep dive: sentinel-infra
§11` (per-resource table).**
