# Sentinel

> [!IMPORTANT]
> ### 🚧 Phase 2 is under active construction
> Sentinel is being rebuilt as a **cloud-native, multi-repo system** on Azure — an AKS
> scale-to-zero backend, Terraform IaC, real Datadog signal from 30 ground-truth scenario
> branches, and **passwordless Entra security end-to-end** (OIDC, workload identity, bearer
> tokens). The README below documents the **Phase 1 MVP**; Phase 2 spans three repos:
>
> | Repo | Role | Link |
> |------|------|------|
> | **Sentinel** _(this repo · default branch)_ | Multi-agent backend + CI/CD — the brain | **https://github.com/Keshav0375/Sentinel** |
> | **Sentinel-infra** | Terraform IaC + identity plane | **https://github.com/Keshav0375/Sentinel-infra** |
> | **Sentinel-deployment** | Target app + 30 scenario branches (ground truth) | **https://github.com/Keshav0375/Sentinel-deployment** |
>
> **The diagram below is the architectural view of Phase 2** — every repo, request, flow, auth
> edge, and CI/CD pipeline in one picture.

## 🏗️ Phase 2 — Full Architecture (every request · flow · pipeline)

```mermaid
flowchart TB
    %% ================= NODES (declared inside their repos) =================
    subgraph INFRA["🏗️ sentinel-infra — Terraform IaC · provisions everything"]
        direction TB
        TF["terraform apply"]
        IDP["Identity plane<br/>OIDC app · backend Entra app<br/>api://sentinel-backend · workload identity<br/>Key Vault rotation"]
        CIINFRA["infra_validate_plan · infra_deploy"]
        DESTROY["ci_destroy_infra<br/>full teardown → az group delete"]
        RUNNERS["infra_runners → CI images"]
    end

    subgraph AZURE["☁️ Azure · resource group sentinel-rg · all free tier"]
        direction TB
        AKS["AKS · scale-to-zero<br/>1× B2ats_v2 node 0↔1 per run"]
        ACR["ACR<br/>backend + runner images"]
        PG[("PostgreSQL B1MS + pgvector<br/>Entra-only auth · no password<br/>incidents · services · deployments")]
        KV["Key Vault<br/>LLM keys ↻ rotated · rotator Fn"]
        EG["Event Grid Topic"]
        FUNC["Function bridge<br/>classifies + stamps signal_type"]
        APP["App Service F1<br/>dummy-api"]
    end

    subgraph DEPLOY["🎯 sentinel-deployment — the target app"]
        direction TB
        BRANCHES["30 scenario branches<br/>pass · deployfail · runtime · 10 each"]
        CIAPP["ci_app_deployment<br/>Build → Deploy → Verify → Record → Datadog"]
    end

    subgraph DD["📊 Datadog"]
        direction TB
        MDEP["monitor · deploy-failure"]
        MRUN["monitor · runtime-health"]
    end

    subgraph BACKEND["🧠 sentinel — backend + CI/CD · the brain"]
        direction TB
        CIVAL["ci_validation<br/>PR fast gate"]
        CIDEP["ci_backend_deployment<br/>build → push → deploy → test → promote"]
        CISCALE["ci_backend_scale<br/>up/down + nightly auto-down"]
        CIINC["ci_incident_response<br/>scale up → fetch ∥ → run → branch → scale down"]
        subgraph PIPE["Agent pipeline · runs on AKS"]
            direction TB
            ORCH["Orchestrator<br/>plans · branches on signal_type"]
            TRIAGE["Triage"]
            ANAL["Analysis"]
            REFLEX["Reflexion<br/>self-critique loop ≤2×"]
            RESOLVE["Resolution<br/>rollback spec"]
            REVER["Reverification"]
            JUDGE["Judge<br/>scores trajectory"]
        end
    end

    LF["📈 LangFuse<br/>traces · prompts · scores"]
    TEAMS["💬 Microsoft Teams"]
    REVERTPR{{"Revert PR on sentinel-deployment<br/>= HITL gate · human merges or closes"}}

    %% ================= PROVISIONING =================
    TF --> IDP
    TF -->|provisions| AKS
    TF --> ACR
    TF --> PG
    TF --> KV
    TF --> EG
    TF --> FUNC
    TF --> APP
    CIINFRA --> TF
    IDP -.->|OIDC · no client secret| AKS

    %% ================= DEPLOY FLOW =================
    BRANCHES --> CIAPP
    CIAPP -->|zip deploy| APP
    CIAPP -->|Entra DB token · deploy row| PG
    CIAPP -->|deploy events + logs| DD
    APP -->|logs + metrics| DD

    %% ================= ALERT ROUTING =================
    MDEP -->|signal_type = deploy_failure| EG
    MRUN -->|signal_type = runtime_error| EG
    EG --> FUNC
    FUNC -->|repository_dispatch + signal_type| CIINC

    %% ================= INCIDENT PIPELINE =================
    CIINC -->|1 · scale backend up| CISCALE
    CISCALE --> AKS
    CIINC -->|3 · POST /webhooks/incident · Authorization: Bearer| ORCH
    ORCH -->|deploy_failure · rollback fast path| RESOLVE
    ORCH -->|runtime_error| TRIAGE
    TRIAGE --> ANAL --> REFLEX
    REFLEX -->|conf ≥ 0.7 + specific deploy| RESOLVE
    REFLEX -->|conf &lt; 0.7 · ambiguous| JUDGE
    RESOLVE --> REVER --> JUDGE

    %% ================= BACKEND DATA + RUNTIME AUTH =================
    ORCH -.->|workload identity → LLM keys| KV
    ORCH -.->|workload identity → Entra DB token| PG
    JUDGE -->|store incident| PG
    JUDGE -->|traces + scores| LF

    %% ================= BACKEND CI/CD =================
    CIVAL -. gates PRs .-> CIDEP
    CIDEP -->|push image| ACR
    CIDEP -->|kubectl deploy| AKS
    ACR -->|kubelet pull · AcrPull| AKS

    %% ================= HITL LOOP =================
    RESOLVE -->|rollback spec| REVERTPR
    REVERTPR -->|human merges → redeploy| BRANCHES
    REVER -.->|escalate · low confidence| TEAMS
    CIINC -->|notify outcome| TEAMS

    %% ================= STYLING =================
    style INFRA fill:#e8f0fe,stroke:#4285f4,color:#1a1a1a
    style AZURE fill:#e6f4ea,stroke:#34a853,color:#1a1a1a
    style DEPLOY fill:#fef7e0,stroke:#fbbc04,color:#1a1a1a
    style DD fill:#f3e8fd,stroke:#a142f4,color:#1a1a1a
    style BACKEND fill:#fce8e6,stroke:#ea4335,color:#1a1a1a
    style PIPE fill:#ffffff,stroke:#ea4335,color:#1a1a1a
    style REVERTPR fill:#fff3cd,stroke:#d39e00,color:#1a1a1a
```

_This is the architectural view of Phase 2._ Deep-dive docs live in the
[**sentinel-brain**](https://github.com/Keshav0375/sentinel-brain) repo —
`architecture/README.md` is the index, `architecture/backend.md` is authoritative for this repo.

---

**Autonomous DevOps incident response agent** — multi-agent pipeline that triages alerts, diagnoses root causes, drafts a rollback or an escalation, and writes the incident summary. The backend never acts on the world: it drafts a rollback spec, GitHub Actions opens the revert PR on Sentinel-deployment, and a human merges (or closes) it.

Built as a portfolio project demonstrating production-grade agentic engineering: multi-agent orchestration, HITL safety, episodic memory, trajectory evaluation, and real-time observability.

---

## How It Works

```
Alert fires
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│          Orchestrator (Python code, not an LLM agent)        │
│  plans on signal_type · reflexion ≤ 2 loops · decision gate  │
│  reverification (1 retry) · hard 20-tool-call budget         │
└──┬──────────┬──────────┬──────────┬──────────┬─────────────┘
   ▼          ▼          ▼          ▼          ▼
Triage    Analysis   Reflexion  Resolution  Reverification
   │          │      (critique,     │       (PASS / FAIL /
get_      fetch_      no tools)  prepare_    ESCALATE,
service_  logs                   rollback_   no tools)
metadata  get_                   spec  or
search_   deploy_                format_
past_     details                escalation
incidents                      │
                               ▼
              Rollback spec ──► GitHub Actions opens the revert PR
                               │
                               ▼
                     A human merges (or closes) it on GitHub
```

Every tool call is captured as a trajectory. After resolution, an LLM judge scores the trajectory across 6 dimensions and writes a report viewable in the web dashboard.

---

## Features

- **Multi-agent pipeline** — 5 pipeline agents (Triage, Analysis, Reflexion, Resolution, Reverification) run by a Python orchestrator that enforces every cap in code; a sixth, the PR content generator, writes the revert PR for `/generate/pr-content`
- **HITL safety gate** — the backend only drafts a rollback spec or an escalation; GitHub Actions opens the revert PR and a human merges or closes it on GitHub. No agent has a tool that acts on the world
- **Episodic memory** — past incidents embedded and stored in Postgres + pgvector; similar incidents surface automatically during triage
- **Semantic memory** — service map, dependency graph, and runbooks held in Postgres
- **Scenario catalog dashboard** — the 30 Sentinel-deployment scenario branches, read-only, at `http://localhost:8000/`
- **Trajectory eval** — LLM-as-judge scores every incident across 6 dimensions (0–5), with a Markdown and JSON report
- **Eval results dashboard** — browse per-scenario scores at `http://localhost:8000/eval`
- **30 scenario branches** — 10 each of case i (healthy or runtime fault), ii (pipeline failure) and iii (post-deploy fault), in Sentinel-deployment

---

## Quick Start

### Prerequisites

- Python 3.12+
- An Anthropic and/or OpenAI API key (not needed to boot; an incident without one escalates)
- Docker (optional: a local Postgres + pgvector for memory and the DB tests)

### 1. Clone and configure

```bash
git clone https://github.com/keshxv/sentinel.git
cd sentinel
cp .env.example .env
# Edit .env — add ANTHROPIC_API_KEY and OPENAI_API_KEY
```

### 2a. Local Postgres (optional)

```bash
docker run -d --name sentinel-pg -e POSTGRES_PASSWORD=test -p 55432:5432 pgvector/pgvector:pg16
```

Set `DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres` in `.env` and run `poetry run alembic upgrade head`. Without `DATABASE_URL` the app still boots; memory reports unavailable.

### 2b. Run

```bash
poetry install
poetry run sentinel serve
```

Dashboard: [http://localhost:8000](http://localhost:8000)

### 3. Switch the primary provider (optional)

Each agent role runs a fixed Anthropic/OpenAI model pair (architecture §4.7, in
`src/sentinel/providers/router.py`). Anthropic runs first by default; a rate limit, timeout
or API error re-runs the agent once on OpenAI. To run OpenAI first:

```env
SENTINEL_PRIMARY_PROVIDER=openai
```

The eval judge always runs on the provider the agents do not use.

### 4. Scenarios

Scenarios are the 30 branches of Sentinel-deployment (`scenarios/branches.yaml`, vendored at `src/sentinel/catalog/branches.yaml`). A human merges a scenario branch's PR; Datadog → bridge → `repository_dispatch` → `ci_incident_response.yml` runs the pipeline. `GET /api/scenarios` (bearer) lists the catalog; the dashboard shows it read-only.

### 5. Run the eval suite

```bash
python3 scripts/run_eval.py --branches ../Sentinel-deployment/scenarios/branches.yaml
# dataset (default sentinel-scenario-runs) and dataset-run name:
python3 scripts/run_eval.py --branches <path> --dataset sentinel-scenario-runs --run-name prompt-v2
python3 scripts/run_eval.py --branches <path> --no-store   # do not write incidents.eval_score
```

Results → `reports/eval_report.json` + `reports/eval_report.md`  
Dashboard → [http://localhost:8000/eval](http://localhost:8000/eval)  
With `DATABASE_URL` set, each judged score is also stored on its incident (`incidents.eval_score`).

---

## Demo

> _Loom recording coming soon._
![Dashboard Image](image.png)
**What you'll see:**

1. Select `bad_deploy_01` from the dropdown — a critical API gateway alert fires
2. Watch the agent cards light up as triage, analysis and the reflexion critique run
3. The Resolution Agent drafts a rollback spec, reverification checks it, and the incident ends `rollback_proposed` (or `escalated`)
4. Switch to `/eval` to see the LLM judge's per-dimension scores

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Agent framework | [openai-agents](https://github.com/openai/openai-agents-python) (Agents SDK) |
| LLM | LiteLLM — Anthropic (default) and OpenAI, a fixed model pair per agent role with automatic fallback (§4.7). |
| API layer | FastAPI + uvicorn |
| Memory — episodic | PostgreSQL + pgvector (asyncpg) |
| Memory — semantic | PostgreSQL (`services` table) |
| Embeddings | sentence-transformers `all-MiniLM-L6-v2` (local, 384 dims, zero cost) |
| Models / validation | Pydantic v2 |
| Config | pydantic-settings |
| Logging | structlog (JSON) |
| HTTP client | httpx |
| Containerisation | Docker |
| Linting | ruff |
| Type checking | pyright |
| Tests | pytest + pytest-asyncio |

---

## Eval Results

> _Run `python scripts/run_eval.py` to generate scores. Results will appear at `/eval`._

**Eval dimensions (scored 0–5 by the judge):**

| Dimension | What it measures |
|-----------|-----------------|
| `triage_accuracy` | Right service + correct severity? |
| `root_cause_correctness` | Hypothesis matches known root cause? |
| `tool_efficiency` | Right tools in logical order? No redundant calls? |
| `mttr` | Time from alert to remediation proposal (lower is better) |
| `remediation_safety` | HITL gate used? Proposed fix appropriate? |
| `comms_quality` | Slack summary clear, complete, structured? |

Pass threshold: **3.0 / 5.0** (60%)

---

## Project Structure

```
sentinel/
├── src/sentinel/
│   ├── agents/          # 6 LLM agents + the Python orchestrator
│   ├── tools/           # Tool implementations (fetch_logs, prepare_rollback_spec, …)
│   ├── memory/          # Episodic (Postgres + pgvector), semantic, short-term
│   ├── eval/            # Rubric, LLM-as-judge, batch runner, report generator
│   ├── api/             # FastAPI routes (webhooks, incidents, generate, scenarios)
│   ├── infra/           # DB, logging, tracing, event bus, dashboard emitter
│   ├── models/          # Pydantic domain models
│   ├── catalog/         # branches.yaml: the vendored scenario catalog
│   └── dashboard/       # index.html (live dashboard) + eval.html
├── scripts/
│   ├── run_eval.py      # Score LangFuse scenario runs against branches.yaml
├── tests/               # ~1 000 tests across all layers
└── reports/             # Trajectory JSON, eval_report.json/md
```

---

## Architecture Decisions

**Why multi-agent, not single agent?** Each specialist has a focused system prompt and constrained tool set. This makes each agent independently testable and improvable — changing the Analysis prompt doesn't risk breaking Triage.

**Why HITL at the tool level, not the prompt level?** There is no "execute rollback" tool — only `prepare_rollback_spec` and `format_escalation`, which draft and return. An agent can't bypass the gate by ignoring a prompt instruction: it has no tool that acts, and the revert PR a human merges on GitHub is the only path to a rollback.

**Why Postgres + pgvector for episodic memory?** One database holds services, deployments and incidents, and pgvector's `<=>` replaces in-process cosine similarity (backend.md §5, §13.2).

**Why a separate judge model?** Using a different model family for evaluation avoids self-grading bias. The agents run on `llama-3.3-70b-versatile`; the judge runs on `llama-3.1-8b-instant`. In production, you'd use a different provider entirely.

---

## Phase 2 Roadmap

_Documented here for interview conversations about production readiness:_

| Capability | Phase 2 approach |
|-----------|-----------------|
| Cloud deployment | Azure Container Apps + Bicep IaC |
| Database | PostgreSQL + pgvector (asyncpg stores, Alembic migrations) |
| Cache | Redis for short-term memory |
| Model routing | LiteLLM done (Phase 9) — Kong AI Gateway for token budgets is Phase 2 |
| Observability | Self-hosted LangFuse for trace dashboards |
| PR creation | GitHub App for real PR creation |
| Self-improvement | Nightly ACA Job — re-evals + auto-PR to improve failing prompts |
| Alert source | Azure Service Bus + Datadog webhook for real alerts |
| Adversarial evals | 40 scenarios, 8 failure classes, ambiguous multi-cause cases |
| CI/CD | GHA with eval score gates (fail deploy if avg < 3.5) |

---

## Running Tests

```bash
pytest                     # run all ~1 000 tests
pytest tests/test_eval/    # eval layer only
pytest tests/test_tools/   # tool layer only
pytest -x -v               # stop on first failure, verbose
```

---

## License

MIT
