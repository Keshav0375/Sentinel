# Category 1 — sentinel-infra

Terraform IaC that provisions the entire Azure stack. **Implemented first** — every
downstream repo depends on the resources, secrets, and OIDC identity created here.

- **Repo:** `Keshav0375/Sentinel-infra` · local `Agentic-Engineering/Sentinel-development-project/Sentinel-infra`
- **Architecture:** [../../Phase-2/sentinel-infra/ARCHITECTURE.md](../../Phase-2/sentinel-infra/ARCHITECTURE.md)
- **Quality gate:** `python scripts/quality_gate.py --repo infra` (fmt · validate · tflint · tfsec · gitleaks)
- **Env:** [../env-examples/infra.env.example](../env-examples/infra.env.example)

> ⚠ Use real GitHub owner `Keshav0375` (not `keshxvDev`) in OIDC subjects and secret
> targets. Most tasks here are **blocked on an Azure account** for verification (code can
> still be written) — see [STATE-IMPL blockers](../STATE-IMPL.md#blockers).

## Phases (each = 1 branch + 1 PR)

| Phase | Branch | Tasks | Arch §§ |
|-------|--------|-------|---------|
| **1 — Foundations & Bootstrap** | `dev/infra-phase-1-foundations` | repo skeleton · state bootstrap · OIDC federation | §2, §4, §8 |
| **2 — Core Resource Modules** | `dev/infra-phase-2-core-modules` | ACR · PostgreSQL · Key Vault | §3.1–3.3 |
| **3 — Compute & Networking** | `dev/infra-phase-3-compute-modules` | AKS (+ workload identity) · Event Grid · Function bridge · App Service · backend Entra app · KV rotation | §3.4–3.8, §4.4 |
| **4 — Cross-Repo Wiring & CI** | `dev/infra-phase-4-wiring-and-ci` | secret distribution · runner image · workflows · root wiring | §5, §6, §7 |

## Tasks

- **1.1** [Repo skeleton + providers](phase-1-foundations/task-1-repo-skeleton-and-providers.md)
- **1.2** [Remote state bootstrap](phase-1-foundations/task-2-remote-state-bootstrap.md)
- **1.3** [OIDC federation](phase-1-foundations/task-3-oidc-federation.md)
- **2.1** [ACR module](phase-2-core-modules/task-1-acr-module.md)
- **2.2** [PostgreSQL module](phase-2-core-modules/task-2-postgresql-module.md)
- **2.3** [Key Vault module](phase-2-core-modules/task-3-keyvault-module.md)
- **3.1** [AKS module (+ workload identity)](phase-3-compute-modules/task-1-aks-module.md)
- **3.2** [Event Grid module](phase-3-compute-modules/task-2-event-grid-module.md)
- **3.3** [Function bridge module](phase-3-compute-modules/task-3-functions-bridge-module.md)
- **3.4** [App Service module](phase-3-compute-modules/task-4-app-service-module.md)
- **3.5** [Backend Entra app registration](phase-3-compute-modules/task-5-backend-entra-app.md) · _rev-5_
- **3.6** [Key Vault rotation Function](phase-3-compute-modules/task-6-keyvault-rotation.md) · _rev-5_
- **4.1** [Cross-repo secrets](phase-4-wiring-and-ci/task-1-cross-repo-secrets.md)
- **4.2** [CI runner image](phase-4-wiring-and-ci/task-2-ci-runner-image.md)
- **4.3** [Infra workflows](phase-4-wiring-and-ci/task-3-infra-workflows.md)
- **4.4** [Root wiring + apply](phase-4-wiring-and-ci/task-4-root-wiring-and-apply.md)
