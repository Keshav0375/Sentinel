# Category 2 — sentinel-deployment

A near-trivial FastAPI app plus the **deploy pipeline that is the real product** — every
PR merge ships structured deploy events/logs to Datadog, generating the signal Sentinel's
agents diagnose. **Implemented second** (needs infra: App Service, Key Vault, PostgreSQL).

- **Repo:** `Keshav0375/Sentinel-deployment` · local `Agentic-Engineering/Sentinel-development-project/Sentinel-deployment`
- **Architecture:** [../../Phase-2/sentinel-deployment/ARCHITECTURE.md](../../Phase-2/sentinel-deployment/ARCHITECTURE.md)
- **Quality gate:** `python scripts/quality_gate.py --repo deployment` (ruff · actionlint · yamllint · gitleaks · pytest)
- **Env:** [../env-examples/deployment.env.example](../env-examples/deployment.env.example)

> Cross-repo dependency: `ci_app_deployment.yml` reuses sentinel's composite actions
> (`get-kv-secrets`, `psql-exec`) — those are built in **backend Phase 7** (task 7.3).
> The record-deployment stage is blocked until infra (PostgreSQL + Key Vault) and those
> actions exist; the pipeline is authored here and wired when its deps are `verified`.

## Phases (each = 1 branch + 1 PR)

| Phase | Branch | Tasks | Arch §§ |
|-------|--------|-------|---------|
| **1 — The App** | `impl/deploy-phase-1-app` | FastAPI app · app tests | §2, §5 |
| **2 — Deploy Pipeline** | `impl/deploy-phase-2-deploy-pipeline` | dd-report action · ci_app_deployment · Datadog monitors | §3, §6.3 |
| **3 — Scenario Branches** | `impl/deploy-phase-3-scenario-branches` | 30 scenario branches (3 cases) + branches.yaml — replaces ci_demo_prs | §4 |

## Tasks

- **1.1** [FastAPI app](phase-1-app/task-1-fastapi-app.md)
- **1.2** [App tests](phase-1-app/task-2-app-tests.md)
- **2.1** [dd-report composite action](phase-2-deploy-pipeline/task-1-dd-report-action.md)
- **2.2** [ci_app_deployment.yml](phase-2-deploy-pipeline/task-2-ci-app-deployment.md)
- **2.3** [Datadog monitors](phase-2-deploy-pipeline/task-3-datadog-monitors.md)
- **3.1** [30 scenario branches + branches.yaml](phase-3-demo-scenarios/task-1-demo-prs-workflow.md)
