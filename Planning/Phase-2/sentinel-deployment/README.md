# sentinel-deployment

Planning for the **sentinel-deployment** repo — a near-trivial FastAPI app deployed to Azure App Service (free tier) via GitHub Actions.

The app itself is a dummy target. The real product is the **deployment pipeline**, which ships structured events and logs to Datadog on every PR merge. Successful deploys, failed builds, broken health checks — all land as real Datadog signal that Sentinel's agents can later analyze.

## What This Repo Will Contain

- Minimal FastAPI app: `GET /`, `GET /health`, `GET /version`
- `ci_app_deployment.yml` — GHA workflow: Build → Deploy → Verify → Report to Datadog
- Demo PRs that create intentional failures across different stages

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Deploy target | Azure App Service F1 | Always-free, no payment method needed |
| App design | Near-trivial (3 endpoints) | Pipeline is the product, not the app |
| Datadog integration | Dual: native Azure + GHA curl | Auto metrics + custom deploy events |
| Deploy method | Zip deploy | F1 tier doesn't support containers; Oryx handles pip install |

## Monitoring

- **Datadog native integration** — auto-collects App Service metrics (CPU, memory, HTTP codes)
- **GHA pipeline → Datadog API** — custom deploy events + structured logs via `curl`

## Key Docs

| Doc | Purpose |
|-----|---------|
| `ARCHITECTURE.md` | Full architecture (pipeline stages, Datadog schema, demo PR sequence) |
| `../reference-documentation/links.md` | Datadog + Azure integration docs and findings |

## Status

Architecture doc finalized (Azure App Service F1 + zip deploy). Waiting on Datadog + Azure setup before building.
