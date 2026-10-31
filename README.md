# Sentinel

[![CI](https://github.com/Keshav0375/Sentinel/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Keshav0375/Sentinel/actions/workflows/ci.yml)
[![Dashboard web CI](https://github.com/Keshav0375/Sentinel/actions/workflows/ci_web.yml/badge.svg?branch=main)](https://github.com/Keshav0375/Sentinel/actions/workflows/ci_web.yml)
[![Ops dashboard](https://img.shields.io/badge/ops%20dashboard-live%20once%20Pages%20is%20enabled-lightgrey)](https://keshav0375.github.io/Sentinel/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Sentinel is an autonomous, multi-agent incident-response system.** When a Datadog alert fires on
a watched service, Sentinel brings its backend up on AKS, triages the alert, finds the deploy that
caused it, critiques its own diagnosis, and drafts a rollback. GitHub Actions turns that draft into
a revert pull request on the target app, and a human merges or closes it. Every step is traced,
every run is scored against a 30-scenario ground truth, and an observe-only ops dashboard shows the
whole chain from build to merge.

> **The backend drafts, GitHub Actions executes, a human approves.** No backend tool can change
> anything outside Sentinel's own database and traces. The revert PR is the only path to a rollback.

Ops dashboard: <https://keshav0375.github.io/Sentinel/> (live once Pages is enabled) · Target app:
[Keshav0375/Sentinel-deployment](https://github.com/Keshav0375/Sentinel-deployment)

---

## How it works

```mermaid
flowchart TD
    DD["Datadog: deploy-failure monitor or runtime-health synthetic"] -->|webhook| EG["Azure Event Grid topic"]
    EG --> FN["Function bridge: classify signal_type"]
    FN -->|"repository_dispatch: incident-alert"| GHA["GHA: ci_incident_response.yml"]
    GHA -->|"scale up, enrich, POST /webhooks/incident with Entra bearer"| TRI

    subgraph AKS["Backend on AKS, namespace sentinel-dev"]
        TRI["Triage"] --> ANA["Analysis"]
        ANA --> REF["Reflexion critique"]
        REF -->|"below 0.7 confidence: loop back, at most 2 times"| ANA
        REF --> GATE{"Decision gate"}
        GATE -->|"0.7 or higher and one specific deploy"| RES["Resolution: RollbackSpec draft"]
        RES --> VER["Reverification"]
        VER -->|"FAIL: one more resolution"| RES
    end

    GATE -->|anything else| ESC["GHA: escalate to Teams"]
    VER -->|"PASS: rollback_proposed"| PRC["GHA: generate PR content, open revert PR"]
    PRC --> PR{{"Revert PR on Sentinel-deployment"}}
    PR -->|"a human merges or closes"| DEPLOY["Target app redeploys the last good commit"]
```

1. **Signal.** The target app's deploy pipeline and Datadog synthetics raise one of two signals. The
   Function bridge only classifies and forwards: an event tagged `deploy_status:failed` becomes
   `deploy_failure`, anything else `runtime_error`.
2. **Executor.** [`ci_incident_response.yml`](.github/workflows/ci_incident_response.yml) scales the
   backend up, fetches the service row, PR details and recent Datadog logs in parallel, and calls the
   backend with an Entra access token.
3. **Reasoning.** The [orchestrator](src/sentinel/agents/orchestrator.py) is plain Python, not an LLM.
   `runtime_error` runs the full loop. `deploy_failure` takes the fast path: one critique, no loop
   back, pinned to the failed deploy's commit.
4. **Human gate.** On `rollback_proposed` the workflow asks the backend for PR text, opens the revert
   PR, records it and notifies Teams. Nothing in the workflow merges, approves or enables auto-merge.
   Then it scales the backend down again, unless keep-warm is set.

---

## Agents and safety invariants

| Agent | Role | Tools |
|---|---|---|
| Triage | Identify the service and severity, recall similar incidents | `get_service_metadata`, `search_past_incidents` |
| Analysis | Correlate the alert with a deploy, read the evidence | `fetch_logs`, `get_deploy_details` |
| Reflexion | Critique the analysis, ask for the next tool call or approve | none |
| Resolution | Draft the rollback or the escalation | `prepare_rollback_spec`, `format_escalation` |
| Reverification | Check the draft against the evidence: PASS, FAIL or ESCALATE | none |
| PR content | Write the revert PR's title and body (`POST /generate/pr-content`) | none |

Each role runs on a fixed model pair, Anthropic first with OpenAI as fallback (or the reverse), via
LiteLLM ([`providers/router.py`](src/sentinel/providers/router.py)). Every cap is enforced in code:

| Invariant | How it holds |
|---|---|
| **Tool-call cap** | At most 20 tool calls per incident (`MAX_TOOL_CALLS_CAP`), fallback re-runs included. Past it a call is refused and the incident escalates. |
| **Reflexion cap** | At most 2 loops from reflexion back to analysis, and one resolution retry after a reverification FAIL. A 900 s wall-clock bound escalates anything slower. |
| **Distinct judge** | The eval judge runs on the non-primary provider, on a model no agent role uses as primary or fallback, checked at import. A run that still used the judge's model is flagged `judge_conflict` and left out of every aggregate. |
| **R14 trace privacy** | LangFuse gets the model, token usage and linked prompt, never inputs or outputs. Raw logs and tool output stay in Postgres. The SDK's default OpenAI span exporter is removed. |
| **Entra bearer auth** | Every route except `/health` and `/ready` needs a v2 Entra token (RS256, `aud`, `iss`, `exp`) carrying the `Incident.Write` app role. Tokens are never logged. |
| **No write tools** | The tools only read (Postgres, Datadog, GitHub PRs) or return a typed draft. The rollback target must match a real `deployments` row, or the incident escalates. |
| **Untrusted data** | Every prompt ends with a preamble that treats logs, PR titles and past incident notes as data, never as instructions. |

The pod has no Kubernetes Secret. It reads its keys from Key Vault, and Postgres and Redis take
Entra tokens, all through workload identity.

---

## Memory

| Layer | Store | What it holds |
|---|---|---|
| Episodic | Postgres `incidents` + pgvector | Every terminal incident with a 384-dim embedding of the observed symptoms (local `all-MiniLM-L6-v2`), searched with `<=>` over an IVFFlat index. Root causes carry provenance (`model_generated: true`). |
| Semantic | Postgres `services` + pgvector | The service registry, dependencies and a markdown runbook per service, found by exact name or by symptom similarity. |
| Deploys | Postgres `deployments` | One row per deploy attempt, written by the target app's pipeline. These are the correlation targets. |
| Checkpoints | Postgres `incident_checkpoints` | A snapshot after each stage, written off the critical path. A restarted pod resumes a stuck incident **once**, keeping every cap counter. A second restart escalates. |
| Cache | Postgres `cache_entries`, Redis in front | Exact-match, SHA-256-keyed results: `fetch_logs` for 5 minutes, deterministic LLM calls for 24 hours. The judge is never cached. |
| Working memory | Azure Managed Redis | Write-through mirror of per-incident short-term memory, plus status events on `sentinel:events`. Entra auth only, behind a private endpoint. |

Redis is optional and holds nothing durable. Without `REDIS_HOST` every feature falls back to
Postgres or to memory in the process.

**Memory survives teardown.** Before any destroy, `infra_deploy.yml` runs
[`infra/scripts/memory-backup.sh`](infra/scripts/memory-backup.sh). It writes a `pg_dump` to a blob
container outside the destroyable estate and keeps the newest 10. A failed backup blocks the
destroy. After apply, [`memory-restore.sh`](infra/scripts/memory-restore.sh) restores the data in
one transaction. Cached rows are never exported.

---

## Evaluation

The ground truth is [`src/sentinel/catalog/branches.yaml`](src/sentinel/catalog/branches.yaml), a
vendored copy of the 30 scenario branches on Sentinel-deployment:

| Case | Branches | What happens | Expected |
|---|---|---|---|
| i: clean pass | `pass/01` to `pass/10` | Green pipeline, healthy app | no incident |
| ii: deploy fails | `deployfail/01` to `deployfail/10` | Red pipeline, deploy-failure monitor | `deploy_failure` → rollback |
| iii: runtime error | `runtime/01` to `runtime/10` | Green pipeline, a runtime-health synthetic fires | `runtime_error` → rollback or escalate |

A human merges a scenario PR and the real path runs. Each terminal incident becomes one item in the
LangFuse dataset `sentinel-scenario-runs`, with raw tool output stripped.
[`scripts/run_eval.py`](scripts/run_eval.py) is a read-only scorer. Per item it runs four checks
(`fired`, `classified`, `correlated`, `revert_target`). Then an LLM judge scores the primary
incident on five 0–1 dimensions: `triage_accuracy`, `root_cause_correctness`, `tool_efficiency`,
`mttr` and `remediation_safety`. The scores go to LangFuse, to `incidents.eval_score`, and to
`reports/eval_report.{json,md}` (served at `GET /api/eval-report`).

The automated run is `ci_eval.yml` (manual dispatch). It pauses the backend, runs `run_eval.py` as
an in-cluster Kubernetes Job under the backend's workload identity, so the LLM keys never leave the
pod, then emits one `eval.scored` per scenario per eval run (the newest item) to the ops dashboard;
older items of a scenario are counted in the run's summary and report. `run_eval.py` also runs by
hand.

LangFuse holds the traces (one root trace per incident), the agent prompts (with a file fallback in
`src/sentinel/agents/prompts/`), the scores and the dataset. Without LangFuse keys every one of
these is a no-op.

---

## Ops dashboard

A static React SPA ([`dashboard-web/`](dashboard-web/README.md)) for builds, deploys, incidents,
agent runs, revert PRs, reviews and merges, linked into chains. It shows:

- KPIs over 24 h, 7 d, 30 d and all time, daily activity, and an eval scorecard
- a **needs-action lane** for revert PRs awaiting review, escalations, and failed builds or deploys
  that raised no incident
- a filterable chain timeline with a drawer per chain (stages, agent decision and confidence, trace
  waterfall, links)

**Store.** The store is an append-only git ledger: one JSON file per event, with a deterministic
`event_id`, on the orphan `ops-ledger` branch. Workflows append events as they happen.
[`ops_reconcile.yml`](.github/workflows/ops_reconcile.yml) runs every 30 minutes and after each
Sentinel workflow. It re-derives the same events from the read-only GitHub and LangFuse APIs, so a
missed write heals and nothing is duplicated. Then it builds the snapshot the page reads. Ledger
writes fail closed: a per-kind allow-list, no raw logs or tool output, free text capped at 500
characters, and gitleaks before upload.

**Observe-only.** No UI action merges or approves anything. The page has no backend, no secrets and
no write path.

**Design.** Light and dark themes, WCAG 2.1 AA (axe in Playwright on every theme and at 360 px).
Lighthouse scores 0.97 performance and 1.00 accessibility (local median of 5 runs). CI fails below
0.90.

---

## Repository layout

Sentinel is a monorepo. The watched target app,
[Sentinel-deployment](https://github.com/Keshav0375/Sentinel-deployment), is a separate repo: its
scenario branches and revert PRs are the human gate.

```text
Sentinel/
├── src/sentinel/
│   ├── agents/        # Python orchestrator + 6 LLM agents, prompts in prompts/*.txt
│   ├── tools/         # read-only and draft-only tools, injected dependencies
│   ├── providers/     # per-role model map, fallback, LLM cache, fake LLM for tests
│   ├── memory/        # episodic, semantic, short-term, checkpoints, cache
│   ├── eval/          # rubric, LLM judge, runner, dataset writer, report
│   ├── api/           # FastAPI routes: webhook, incidents, PR content, scenarios, health
│   ├── infra/         # database, Entra auth for Postgres/Redis, tracing, resume, logging
│   ├── models/        # Pydantic v2 models for every boundary
│   ├── catalog/       # branches.yaml, the vendored 30-scenario ground truth
│   └── dashboard/     # in-app scenario catalog and eval pages
├── alembic/           # Postgres schema migrations (pgvector, seed services, memory tables)
├── tests/             # pytest suite mirroring src/, plus ops, workflow and script tests
├── scripts/           # run_eval.py, incident_dispatch.py, quality_gate.py, install_locked.py
│   └── ops/           # ops ledger, reconciler, snapshot builder, site check, JSON schemas
├── dashboard-web/     # ops dashboard SPA: React 18, Vite, Vitest, Playwright
├── azure/k8s/         # backend Deployment, Service, NetworkPolicy, ConfigMap (envsubst)
├── infra/             # Terraform: 8 modules, deployment config, ops scripts, runner image
├── .github/
│   ├── workflows/     # backend, incident, ops and infra pipelines
│   └── actions/       # backend-up/down, tokens, Teams, ops-event composite actions
└── Dockerfile         # linux/arm64 backend image, non-root
```

---

## Infrastructure and cost

All of it is Terraform in [`infra/`](infra/README.md). The subscription is a 6-vCPU student grant
and must be able to reach zero cost on demand.

| Component | Size | Cost |
|---|---|---|
| AKS | Free control-plane tier, one `Standard_B2pls_v2` (ARM64) node shared by deployments | Pod-level scale-to-zero after every run plus a nightly scale-down. `infra_pause.yml` stops compute. |
| App Service | F1, the target app | Free tier |
| Functions | Y1 consumption: the Datadog bridge and the Key Vault key rotator | Free grant (1M executions/mo) |
| Postgres Flexible Server | B1ms + 32 GB, Entra-only auth | about **$12/mo** on-demand in East US (up to about $26 elsewhere), while the estate is up |
| Azure Managed Redis | Balanced B0, single node, HA off | **$13.14/mo** while the deployment exists |
| Redis private endpoint | one per deployment | about **$7.30/mo** (Azure list price) |
| `memory-backups` blobs | 10 dumps per deployment, Standard LRS | **cents/mo**, survives destroy |

The figures are approximate Azure list prices from [`infra/docs/COSTS.md`](infra/docs/COSTS.md).
Check your region before relying on them. `infra_deploy.yml` with `destroy` leaves only the
bootstrap state group, which costs $0 apart from the backup blobs. The runbook is
[`infra/docs/BOOTSTRAP.md`](infra/docs/BOOTSTRAP.md).

---

## Running it locally

You need Python 3.12 or 3.13, [Poetry](https://python-poetry.org/), Docker, and Node 22 with
Corepack for the dashboard. No key is needed to boot: the fake LLM runs the real agent loop with no
provider.

```bash
poetry install
cp .env.example .env          # every variable is optional; see the comments

# Postgres + pgvector
docker run -d --name sentinel-pg-test -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=sentinel -p 55432:5432 pgvector/pgvector:pg16
export DATABASE_URL=postgresql://postgres:test@localhost:55432/sentinel
poetry run alembic upgrade head

# The backend on http://localhost:8000 (deterministic fake LLM, no API keys)
SENTINEL_FAKE_LLM=1 poetry run sentinel serve

# Tests (the DB tests skip without TEST_DATABASE_URL)
poetry run pytest
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/sentinel poetry run pytest

# Ops dashboard on http://localhost:5173/Sentinel/ (serves the fixtures)
corepack pnpm@10.34.9 --dir dashboard-web install --frozen-lockfile
corepack pnpm@10.34.9 --dir dashboard-web dev
```

Protected routes return 401 until `SENTINEL_API_TENANT_ID` and `SENTINEL_API_AUDIENCE` are set.
`SENTINEL_DEV_OPEN_DASHBOARD=1` opens only the in-app catalog and eval pages and their read APIs.

---

## Quality gates and CI

The suite has **2,693 pytest tests** (`pytest --collect-only`), **67 Vitest tests** and **18
Playwright runs** (6 smoke tests × light, dark and 360 px). [`scripts/quality_gate.py`](scripts/quality_gate.py)
is the one gate body that CI and local checks share. For the backend it runs ruff, pyright (strict
on `src/`), gitleaks, pip-audit and pytest. For `infra/` it runs fmt, validate, tflint, tfsec,
`terraform test`, shellcheck and actionlint.

| Workflow | Trigger | What it does |
|---|---|---|
| [`ci.yml`](.github/workflows/ci.yml) | PR | Branch-name convention, secret leak check, ruff, pyright, pytest against a pgvector service |
| [`ci_validation.yml`](.github/workflows/ci_validation.yml) | PR touching the backend | Fast gate with no Azure and no secrets: quality checks, then builds the image and tests it |
| [`ci_backend_deployment.yml`](.github/workflows/ci_backend_deployment.yml) | push to `main` | Quality → immutable `sha-<7>` image in ACR → deploy to AKS → tests and a live smoke incident → promote or roll back → scale down |
| [`ci_incident_response.yml`](.github/workflows/ci_incident_response.yml) | `repository_dispatch` | The incident pipeline above: scale up, enrich, run, revert PR or escalation, scale down |
| [`ci_backend_scale.yml`](.github/workflows/ci_backend_scale.yml) | manual, nightly | Scale the backend up or down. The 02:00 UTC run always scales to zero. |
| [`guard-main-source.yml`](.github/workflows/guard-main-source.yml) | PR | Only `dev/*` and `fix/*` branches may merge into `main` |
| [`ci_web.yml`](.github/workflows/ci_web.yml) | PR / push touching `dashboard-web/` | Typecheck, lint, Vitest, build, site check, Playwright + axe, Lighthouse CI |
| [`ops_reconcile.yml`](.github/workflows/ops_reconcile.yml) | every 30 min, after Sentinel runs | Backfill the ops ledger, build and scan the snapshot |
| [`ops_pages.yml`](.github/workflows/ops_pages.yml) | after reconcile or web CI | Check the site and publish the dashboard to GitHub Pages |
| [`infra_validate_plan.yml`](.github/workflows/infra_validate_plan.yml) | push / PR touching `infra/` | Validate on every push, `terraform plan` where a credential exists |
| [`infra_deploy.yml`](.github/workflows/infra_deploy.yml) | manual | Plan, apply or destroy behind GitHub environments, with memory backup and restore |
| [`infra_pause.yml`](.github/workflows/infra_pause.yml) | manual | Pause or resume compute under a least-privilege identity |
| [`infra_runners.yml`](.github/workflows/infra_runners.yml) | push touching `infra/ci-images/` | Build the CI runner image and push it to ACR |

Every cloud workflow signs in with GitHub OIDC (federated credentials, no client secrets).

---

## Tech stack

| Layer | Choice |
|---|---|
| Agents | [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) + LiteLLM (Anthropic Claude, OpenAI GPT) |
| API | FastAPI, uvicorn, Pydantic v2, pydantic-settings, structlog, httpx |
| Data | PostgreSQL 16 + pgvector, asyncpg, Alembic, sentence-transformers |
| Cache and working memory | Azure Managed Redis (redis-py, Entra tokens) |
| Observability | LangFuse v3 (traces, prompts, scores, datasets), Datadog |
| Auth | Microsoft Entra ID: bearer tokens, workload identity, GitHub OIDC, Key Vault |
| Platform | AKS, ACR, App Service, Azure Functions, Event Grid, Terraform |
| Dashboard | React 18, TypeScript, Vite, Vitest, Testing Library, Playwright, axe, Lighthouse CI |
| Tooling | Poetry, ruff, pyright, pytest, gitleaks, pip-audit, tflint, tfsec, actionlint |

---

## License

[MIT](LICENSE) © 2026 Keshav
