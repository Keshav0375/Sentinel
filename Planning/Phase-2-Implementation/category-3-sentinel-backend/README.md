# Category 3 — sentinel-backend

The multi-agent incident-response brain — the `Sentinel` repo IS the backend.
**Implemented third**, once infra + deployment provide real ground truth. This category is
a **migration** of the working Phase-1 codebase to Phase-2 (arch §13); tasks follow the
prescribed migration order so Phase-1 keeps working until Phase-2 is proven.

- **Repo:** `Keshav0375/Sentinel` (this repo) · local `Agentic-Engineering/Sentinel`
- **Architecture:** [../../Phase-2/sentinel/ARCHITECTURE.md](../../Phase-2/sentinel/ARCHITECTURE.md) · cleanup plan §13
- **Quality gate:** `python scripts/quality_gate.py --repo backend` (ruff · pyright · gitleaks · pip-audit · pytest unit+integration)
- **Env:** [../env-examples/backend.env.example](../env-examples/backend.env.example)

> Follows arch §13.5 migration order: add deps → database.py → alembic → memory → tools →
> orchestrator → LangFuse → **delete Phase-1 artifacts last** (Phase 9). Tests use a local
> `pgvector/pgvector:pg16` container and `SENTINEL_FAKE_LLM=1` — no cloud needed for CI.

## Phases (each = 1 branch + 1 PR)

| Phase | Branch | Tasks | Arch §§ |
|-------|--------|-------|---------|
| **1 — Data Layer** | `impl/backend-phase-1-data-layer` | deps · asyncpg pool · alembic schema · seed | §5, §11, §13.3 |
| **2 — Memory & Tracing** | `impl/backend-phase-2-memory-and-tracing` | episodic · semantic · embeddings · LangFuse | §5, §6, §13.2 |
| **3 — Tools** | `impl/backend-phase-3-tools` | config · 5 tool rewrites · remove 2 tools | §3.4, §4.8, §13.2 |
| **4 — Agents** | `impl/backend-phase-4-agents` | provider routing · orchestrator loops · prompts · pr-content agent | §4, §3.4 |
| **5 — API** | `impl/backend-phase-5-api` | webhook · queries · generate · probes · lifespan | §3 |
| **6 — Eval** | `impl/backend-phase-6-eval` | judge → LangFuse · runner → datasets | §6.4 |
| **7 — Container & K8s** | `impl/backend-phase-7-container-and-k8s` | Dockerfile · manifests · composite actions | §7, §8, §9 |
| **8 — CI/CD** | `impl/backend-phase-8-cicd` | validation · deployment · incident · scale | §9 |
| **9 — Cleanup & E2E** | `impl/backend-phase-9-cleanup` | delete Phase-1 · end-to-end smoke + eval | §13.1, §13.4 |

## Tasks

**Phase 1** · [1.1 deps](phase-1-data-layer/task-1-phase2-dependencies.md) · [1.2 asyncpg pool](phase-1-data-layer/task-2-asyncpg-database-pool.md) · [1.3 alembic schema](phase-1-data-layer/task-3-alembic-initial-schema.md) · [1.4 seed services](phase-1-data-layer/task-4-seed-services-migration.md)
**Phase 2** · [2.1 episodic](phase-2-memory-and-tracing/task-1-episodic-memory.md) · [2.2 semantic](phase-2-memory-and-tracing/task-2-semantic-memory.md) · [2.3 embeddings](phase-2-memory-and-tracing/task-3-embeddings.md) · [2.4 langfuse](phase-2-memory-and-tracing/task-4-langfuse-tracing.md)
**Phase 3** · [3.1 config](phase-3-tools/task-1-config-extensions.md) · [3.2 service_lookup](phase-3-tools/task-2-service-lookup.md) · [3.3 incident_search](phase-3-tools/task-3-incident-search.md) · [3.4 log_fetcher](phase-3-tools/task-4-log-fetcher.md) · [3.5 deploy_checker](phase-3-tools/task-5-deploy-checker.md) · [3.6 remediation+escalation](phase-3-tools/task-6-remediation-and-escalation.md) · [3.7 remove tools](phase-3-tools/task-7-remove-comms-and-hitl-tools.md)
**Phase 4** · [4.1 providers](phase-4-agents/task-1-provider-routing.md) · [4.2 orchestrator](phase-4-agents/task-2-orchestrator-loops.md) · [4.3 prompts](phase-4-agents/task-3-reflexion-reverification-prompts.md) · [4.4 pr-content](phase-4-agents/task-4-pr-content-generator.md)
**Phase 5** · [5.1 webhook](phase-5-api/task-1-webhook-receiver.md) · [5.2 queries](phase-5-api/task-2-incident-query-endpoints.md) · [5.3 generate](phase-5-api/task-3-generate-pr-content-endpoint.md) · [5.4 probes](phase-5-api/task-4-health-ready-probes.md) · [5.5 lifespan](phase-5-api/task-5-app-lifespan-and-concurrency.md)
**Phase 6** · [6.1 judge](phase-6-eval/task-1-judge-langfuse-scores.md) · [6.2 runner](phase-6-eval/task-2-eval-runner-datasets.md)
**Phase 7** · [7.1 dockerfile](phase-7-container-and-k8s/task-1-dockerfile-update.md) · [7.2 manifests](phase-7-container-and-k8s/task-2-k8s-manifests.md) · [7.3 composite actions](phase-7-container-and-k8s/task-3-composite-actions.md)
**Phase 8** · [8.1 validation](phase-8-cicd/task-1-ci-validation.md) · [8.2 deployment](phase-8-cicd/task-2-ci-backend-deployment.md) · [8.3 incident](phase-8-cicd/task-3-ci-incident-response.md) · [8.4 scale](phase-8-cicd/task-4-ci-backend-scale.md)
**Phase 9** · [9.1 cleanup](phase-9-cleanup/task-1-remove-phase1-artifacts.md) · [9.2 end-to-end](phase-9-cleanup/task-2-end-to-end-integration.md)
