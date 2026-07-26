# Sentinel Phase 2 — Master TODO

> Lean tracker: **heading · file · status**. Full detail is in each task file.
> Order is dependency order — work top to bottom. Each **phase = 1 branch + 1 PR**;
> the phase gate (human sign-off) unlocks the next phase. See [README](README.md).
>
> **Branch model:** every phase branches from an up-to-date **`release-phase-2`** as
> `dev/<cat>-phase-<M>-<slug>` and PRs back into `release-phase-2`. `release-phase-2` → `main`
> is one final merge at the end of Phase 2. Never PR a `dev/*` branch to `main` — the
> `verify-source-branch` check rejects it. See [README §6](README.md#6-git-model--one-branch--one-pr-per-phase).
>
> **Status:** ⬜ not-started · 🔵 in-progress · ⛔ blocked · 🟡 done-pending-review · ✅ verified
> **Legend:** 🔒 = phase locked until the previous phase gate is signed off.

**Overall:** 0 / 58 tasks verified · 0 / 16 phases merged.
> **rev-5 (2026-07-12):** tasks added/modified for the security + ground-truth overhaul —
> Entra-only Postgres, Key Vault rotation, backend Entra app + AKS workload identity,
> `ci_destroy_infra`, 30 scenario branches (replaces demo PRs), inbound Entra bearer auth.
> See [../Phase-2/STATE.md](../Phase-2/STATE.md) decision log.

---

## Category 1 — sentinel-infra  ·  `Keshav0375/Sentinel-infra`
_Implement first — provisions the ground truth every other repo depends on._ · [Index](category-1-sentinel-infra/README.md)

### Phase 1 — Foundations & Bootstrap  ·  branch `dev/infra-phase-1-foundations`  ·  Gate: ⬜
| # | Task | File | Status |
|---|------|------|--------|
| 1.1 | Repo skeleton + provider/backend/vars config | [task-1](category-1-sentinel-infra/phase-1-foundations/task-1-repo-skeleton-and-providers.md) | ⬜ |
| 1.2 | Remote state bootstrap (script + doc) | [task-2](category-1-sentinel-infra/phase-1-foundations/task-2-remote-state-bootstrap.md) | ⬜ |
| 1.3 | OIDC federation (bootstrap + TF federated creds) | [task-3](category-1-sentinel-infra/phase-1-foundations/task-3-oidc-federation.md) | ⬜ |

### Phase 2 — Core Resource Modules  ·  branch `dev/infra-phase-2-core-modules`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 2.1 | ACR module | [task-1](category-1-sentinel-infra/phase-2-core-modules/task-1-acr-module.md) | ⬜ |
| 2.2 | PostgreSQL module (+ pgvector, **Entra-only auth** + admin group, firewall) | [task-2](category-1-sentinel-infra/phase-2-core-modules/task-2-postgresql-module.md) | ⬜ |
| 2.3 | Key Vault module (+ RBAC roles, secrets — **no db-password/api-token**) | [task-3](category-1-sentinel-infra/phase-2-core-modules/task-3-keyvault-module.md) | ⬜ |

### Phase 3 — Compute & Networking Modules  ·  branch `dev/infra-phase-3-compute-modules`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 3.1 | AKS module (+ AcrPull, scale-to-zero, **workload identity: OIDC issuer + backend UAMI + federated cred**) | [task-1](category-1-sentinel-infra/phase-3-compute-modules/task-1-aks-module.md) | ⬜ |
| 3.2 | Event Grid module (+ **two-signal routing**) | [task-2](category-1-sentinel-infra/phase-3-compute-modules/task-2-event-grid-module.md) | ⬜ |
| 3.3 | Azure Function bridge module (+ Python source, **stamps `signal_type`**) | [task-3](category-1-sentinel-infra/phase-3-compute-modules/task-3-functions-bridge-module.md) | ⬜ |
| 3.4 | App Service module (F1 target) | [task-4](category-1-sentinel-infra/phase-3-compute-modules/task-4-app-service-module.md) | ⬜ |
| 3.5 | **Identity plane** — backend Entra app (`api://sentinel-backend` + `Incident.Write`) + role grant to `sentinel-gha` | [task-5](category-1-sentinel-infra/phase-3-compute-modules/task-5-backend-entra-app.md) | ⬜ |
| 3.6 | **Key Vault rotation** — rotation policy + rotator Function (`SecretNearExpiry` → new version) | [task-6](category-1-sentinel-infra/phase-3-compute-modules/task-6-keyvault-rotation.md) | ⬜ |

### Phase 4 — Cross-Repo Wiring & CI  ·  branch `dev/infra-phase-4-wiring-and-ci`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 4.1 | Cross-repo secret/variable distribution (github provider — vars + `SENTINEL_API_AUDIENCE`, **no DB_PASSWORD**) | [task-1](category-1-sentinel-infra/phase-4-wiring-and-ci/task-1-cross-repo-secrets.md) | ⬜ |
| 4.2 | CI runner image (Dockerfile + build-push) | [task-2](category-1-sentinel-infra/phase-4-wiring-and-ci/task-2-ci-runner-image.md) | ⬜ |
| 4.3 | Infra workflows (dry / apply / **destroy** / runners) | [task-3](category-1-sentinel-infra/phase-4-wiring-and-ci/task-3-infra-workflows.md) | ⬜ |
| 4.4 | Root wiring + outputs + end-to-end apply | [task-4](category-1-sentinel-infra/phase-4-wiring-and-ci/task-4-root-wiring-and-apply.md) | ⬜ |

---

## Category 2 — sentinel-deployment  ·  `Keshav0375/Sentinel-deployment`
_Implement second — the target app + deploy pipeline that generates real Datadog signal._ · [Index](category-2-sentinel-deployment/README.md)

### Phase 1 — The App  ·  branch `dev/deploy-phase-1-app`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 1.1 | FastAPI app (3 routes + startup log + config) | [task-1](category-2-sentinel-deployment/phase-1-app/task-1-fastapi-app.md) | ⬜ |
| 1.2 | App tests (health / version / root) | [task-2](category-2-sentinel-deployment/phase-1-app/task-2-app-tests.md) | ⬜ |

### Phase 2 — Deploy Pipeline  ·  branch `dev/deploy-phase-2-deploy-pipeline`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 2.1 | `dd-report` composite action | [task-1](category-2-sentinel-deployment/phase-2-deploy-pipeline/task-1-dd-report-action.md) | ⬜ |
| 2.2 | `ci_app_deployment.yml` (Build→Deploy→Verify→Record[**Entra DB token**]→Summary) | [task-2](category-2-sentinel-deployment/phase-2-deploy-pipeline/task-2-ci-app-deployment.md) | ⬜ |
| 2.3 | Datadog monitors (deploy-failure → `deploy_failure`; runtime-health → `runtime_error`) | [task-3](category-2-sentinel-deployment/phase-2-deploy-pipeline/task-3-datadog-monitors.md) | ⬜ |

### Phase 3 — Scenario Branches  ·  branch `dev/deploy-phase-3-scenario-branches`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 3.1 | **30 scenario branches** (10 per case) + `scenarios/branches.yaml` catalog — replaces `ci_demo_prs.yml` | [task-1](category-2-sentinel-deployment/phase-3-demo-scenarios/task-1-demo-prs-workflow.md) | ⬜ |

---

## Category 3 — sentinel-backend  ·  `Keshav0375/Sentinel` (this repo)
_Implement third — the multi-agent brain. Migrates Phase-1 code to Phase-2 (see arch §13)._ · [Index](category-3-sentinel-backend/README.md)

### Phase 1 — Data Layer Foundation  ·  branch `dev/backend-phase-1-data-layer`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 1.1 | Add Phase-2 deps (asyncpg, pgvector, alembic, langfuse, **pyjwt[crypto], azure-identity, azure-keyvault-secrets**) | [task-1](category-3-sentinel-backend/phase-1-data-layer/task-1-phase2-dependencies.md) | ⬜ |
| 1.2 | asyncpg pool — `infra/database.py` | [task-2](category-3-sentinel-backend/phase-1-data-layer/task-2-asyncpg-database-pool.md) | ⬜ |
| 1.3 | Alembic setup + `001_initial_schema` (3 tables + pgvector) | [task-3](category-3-sentinel-backend/phase-1-data-layer/task-3-alembic-initial-schema.md) | ⬜ |
| 1.4 | `002_seed_services` migration | [task-4](category-3-sentinel-backend/phase-1-data-layer/task-4-seed-services-migration.md) | ⬜ |

### Phase 2 — Memory & Tracing Rewrite  ·  branch `dev/backend-phase-2-memory-and-tracing`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 2.1 | `memory/episodic.py` (asyncpg + pgvector) | [task-1](category-3-sentinel-backend/phase-2-memory-and-tracing/task-1-episodic-memory.md) | ⬜ |
| 2.2 | `memory/semantic.py` (asyncpg) | [task-2](category-3-sentinel-backend/phase-2-memory-and-tracing/task-2-semantic-memory.md) | ⬜ |
| 2.3 | `memory/embeddings.py` (pgvector `<=>`) | [task-3](category-3-sentinel-backend/phase-2-memory-and-tracing/task-3-embeddings.md) | ⬜ |
| 2.4 | `infra/tracing.py` → LangFuse (observe + prompts + scoring) | [task-4](category-3-sentinel-backend/phase-2-memory-and-tracing/task-4-langfuse-tracing.md) | ⬜ |

### Phase 3 — Tools Rewrite  ·  branch `dev/backend-phase-3-tools`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 3.1 | `config.py` extensions (DB / Datadog / Teams / LangFuse / pool) | [task-1](category-3-sentinel-backend/phase-3-tools/task-1-config-extensions.md) | ⬜ |
| 3.2 | `get_service_metadata` → PostgreSQL | [task-2](category-3-sentinel-backend/phase-3-tools/task-2-service-lookup.md) | ⬜ |
| 3.3 | `search_past_incidents` → pgvector | [task-3](category-3-sentinel-backend/phase-3-tools/task-3-incident-search.md) | ⬜ |
| 3.4 | `fetch_logs` → Datadog Logs API | [task-4](category-3-sentinel-backend/phase-3-tools/task-4-log-fetcher.md) | ⬜ |
| 3.5 | `get_deploy_details` → PostgreSQL + GitHub API | [task-5](category-3-sentinel-backend/phase-3-tools/task-5-deploy-checker.md) | ⬜ |
| 3.6 | `prepare_rollback_spec` + `format_escalation` | [task-6](category-3-sentinel-backend/phase-3-tools/task-6-remediation-and-escalation.md) | ⬜ |
| 3.7 | Remove `comms_tools.py` + `hitl.py` | [task-7](category-3-sentinel-backend/phase-3-tools/task-7-remove-comms-and-hitl-tools.md) | ⬜ |

### Phase 4 — Agent Pipeline & Loops  ·  branch `dev/backend-phase-4-agents`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 4.1 | Provider routing + fallback (`providers/`) | [task-1](category-3-sentinel-backend/phase-4-agents/task-1-provider-routing.md) | ⬜ |
| 4.2 | Orchestrator: plan-execute + reflexion + reverification + budget | [task-2](category-3-sentinel-backend/phase-4-agents/task-2-orchestrator-loops.md) | ⬜ |
| 4.3 | `reflexion.txt` + `reverification.txt` prompts | [task-3](category-3-sentinel-backend/phase-4-agents/task-3-reflexion-reverification-prompts.md) | ⬜ |
| 4.4 | `pr_content_generator` agent + prompt | [task-4](category-3-sentinel-backend/phase-4-agents/task-4-pr-content-generator.md) | ⬜ |

### Phase 5 — API & App  ·  branch `dev/backend-phase-5-api`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 5.1 | `POST /webhooks/incident` receiver (+ **`signal_type` two-case branch**) | [task-1](category-3-sentinel-backend/phase-5-api/task-1-webhook-receiver.md) | ⬜ |
| 5.2 | `GET /incidents*` query endpoints | [task-2](category-3-sentinel-backend/phase-5-api/task-2-incident-query-endpoints.md) | ⬜ |
| 5.3 | `POST /generate/pr-content` endpoint | [task-3](category-3-sentinel-backend/phase-5-api/task-3-generate-pr-content-endpoint.md) | ⬜ |
| 5.4 | `/health` + `/ready` probes (open, no auth) | [task-4](category-3-sentinel-backend/phase-5-api/task-4-health-ready-probes.md) | ⬜ |
| 5.5 | `main.py` lifespan + concurrency + **workload-identity KV/DB wiring** | [task-5](category-3-sentinel-backend/phase-5-api/task-5-app-lifespan-and-concurrency.md) | ⬜ |
| 5.6 | **`api/auth.py`** — Entra bearer validation (JWKS: aud/iss/exp/`Incident.Write`); apply to non-health routes | [task-6](category-3-sentinel-backend/phase-5-api/task-6-entra-bearer-auth.md) | ⬜ |

### Phase 6 — Eval  ·  branch `dev/backend-phase-6-eval`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 6.1 | `eval/judge.py` → LangFuse scores | [task-1](category-3-sentinel-backend/phase-6-eval/task-1-judge-langfuse-scores.md) | ⬜ |
| 6.2 | `eval/runner.py` → deploy **30 scenario branches**, score vs `branches.yaml` ground truth → LangFuse | [task-2](category-3-sentinel-backend/phase-6-eval/task-2-eval-runner-datasets.md) | ⬜ |

### Phase 7 — Container & K8s  ·  branch `dev/backend-phase-7-container-and-k8s`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 7.1 | Dockerfile update (drop `data/`, torch-cpu) | [task-1](category-3-sentinel-backend/phase-7-container-and-k8s/task-1-dockerfile-update.md) | ⬜ |
| 7.2 | `azure/k8s/` deployment + **serviceaccount (workload identity)** + service manifests (ConfigMap, not Secret) | [task-2](category-3-sentinel-backend/phase-7-container-and-k8s/task-2-k8s-manifests.md) | ⬜ |
| 7.3 | Composite actions (backend-up/down, kv-secrets, **get-db-token, get-backend-token**, teams, psql) | [task-3](category-3-sentinel-backend/phase-7-container-and-k8s/task-3-composite-actions.md) | ⬜ |

### Phase 8 — CI/CD Workflows  ·  branch `dev/backend-phase-8-cicd`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 8.1 | `ci_validation.yml` (fast PR gate) | [task-1](category-3-sentinel-backend/phase-8-cicd/task-1-ci-validation.md) | ⬜ |
| 8.2 | `ci_backend_deployment.yml` (post-merge deploy) | [task-2](category-3-sentinel-backend/phase-8-cicd/task-2-ci-backend-deployment.md) | ⬜ |
| 8.3 | `ci_incident_response.yml` (real pipeline) | [task-3](category-3-sentinel-backend/phase-8-cicd/task-3-ci-incident-response.md) | ⬜ |
| 8.4 | `ci_backend_scale.yml` (scale toggle + nightly) | [task-4](category-3-sentinel-backend/phase-8-cicd/task-4-ci-backend-scale.md) | ⬜ |

### Phase 9 — Cleanup & End-to-End  ·  branch `dev/backend-phase-9-cleanup`  ·  Gate: 🔒
| # | Task | File | Status |
|---|------|------|--------|
| 9.1 | Remove Phase-1 artifacts + deps (data/, generator/, aiosqlite…) | [task-1](category-3-sentinel-backend/phase-9-cleanup/task-1-remove-phase1-artifacts.md) | ⬜ |
| 9.2 | End-to-end integration smoke + eval | [task-2](category-3-sentinel-backend/phase-9-cleanup/task-2-end-to-end-integration.md) | ⬜ |

---

## Phase Gate Ledger
Recorded in [STATE-IMPL.md](STATE-IMPL.md). A phase is `verified` only after the user
confirms the feature works and the PR is merged to main.
