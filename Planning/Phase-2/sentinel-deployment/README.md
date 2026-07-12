# sentinel-deployment

Planning for the **sentinel-deployment** repo — a near-trivial FastAPI app deployed to Azure App Service (free tier) via GitHub Actions.

The app is the system's **real ground truth** — genuinely deployed, genuinely shipping structured events and logs to Datadog on every deploy. The **deployment pipeline** is the product; **30 pre-authored scenario branches** drive three outcomes (clean pass / deploy failure / green-deploy-but-runtime-error) so successful deploys, failed builds, and broken runtimes all land as real Datadog signal that Sentinel's agents analyze — and that signal set doubles as the eval dataset. (Supersedes the removed `ci_demo_prs.yml` template model.)

## What This Repo Will Contain

- Minimal FastAPI app: `GET /`, `GET /health`, `GET /version`
- `ci_app_deployment.yml` — GHA workflow: Build → Deploy → Verify → Record (PostgreSQL, Entra token) → Report to Datadog
- **30 scenario branches** (10 per case) catalogued in `scenarios/branches.yaml` — the real ground truth **and** the eval dataset (replaces the removed `ci_demo_prs.yml`)
- Three cases: **i** clean pass (no incident, no monitor), **ii** deploy fails → previous version stays live → `deploy-failure` monitor → `signal_type=deploy_failure` → backend **rollback**, **iii** green deploy but runtime error → `runtime-health` monitor → `signal_type=runtime_error` → **full incident response**

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Deploy target | Azure App Service F1 | Always-free, no payment method needed |
| App design | Near-trivial (3 endpoints) | Pipeline is the product, not the app |
| Datadog integration | Dual: native Azure + GHA curl | Auto metrics + custom deploy events |
| Deploy method | Zip deploy | F1 tier doesn't support containers; Oryx handles pip install |
| Deploy recording | psql INSERT into Sentinel PostgreSQL (Stage 5, `if: always()`, **Entra DB token**) | Agents correlate incidents against real deploy rows — failed deploys matter most |
| Scenario surface | **30 branches** (10 per case) in `scenarios/branches.yaml` | Replayable, labelled ground truth = eval dataset; replaces ci_demo_prs |
| Signal types | `deploy_failure` (case ii) / `runtime_error` (case iii) | Bridge stamps `signal_type`; drives the backend's two handling paths |
| Auth | OIDC federation (no client secret); Entra DB token for psql | Provisioned by sentinel-infra Terraform; no `db-password` |

## Monitoring

- **Datadog native integration** — auto-collects App Service metrics (CPU, memory, HTTP codes)
- **GHA pipeline → Datadog API** — custom deploy events + structured logs via `curl`

## Key Docs

| Doc | Purpose |
|-----|---------|
| `ARCHITECTURE.md` | Full architecture (pipeline stages, Datadog schema, 30 scenario branches / 3 cases) |
| `../reference-documentation/links.md` | Datadog + Azure integration docs and findings |

## Status

Architecture doc finalized (**rev 4 — 2026-07-12**): `ci_demo_prs.yml` removed;
**30 scenario branches (3 cases)** are the ground truth + eval dataset; two Datadog
signal types drive the backend's rollback vs full-incident paths; record-deployment
uses an Entra DB token (no `db-password`). Base stack unchanged (App Service F1 + zip
deploy). Waiting on Datadog + Azure setup before building.
