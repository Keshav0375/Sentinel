# sentinel-backend

Planning for the **sentinel-backend** — the intelligence layer of Sentinel, deployed as a standalone API on Azure.

This is the brain: multi-agent orchestration, episodic memory, LLM routing, eval scoring. It receives incident data from the GHA orchestration layer, runs the agent pipeline, and returns structured decisions.

## What This Will Contain

- FastAPI service with the full agent pipeline
- Multi-model LLM routing (Groq primary, Gemini backup)
- PostgreSQL-backed episodic + semantic memory
- LangFuse integration for LLM tracing
- HITL approval endpoints
- Health/readiness probes

## Agent Pipeline

```
Incoming incident
       │
       ▼
  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
  │  TRIAGE  │───►│ ANALYSIS │───►│RESOLUTION│───►│  JUDGE   │
  │  Agent   │    │  Agent   │    │  Agent   │    │  Agent   │
  └──────────┘    └──────────┘    └──────────┘    └──────────┘
       │               │               │               │
       ▼               ▼               ▼               ▼
                    Memory Store (PostgreSQL)
```

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| LLM routing | Groq primary, Gemini backup | Free tier, already working |
| Database | Azure PostgreSQL B1MS | Free 12 months, replaces SQLite |
| Tracing | LangFuse cloud | Free 50K observations/month |
| Hosting | TBD | Open decision — App Service vs AKS vs Functions |

## Deploy Target

Azure App Service or AKS (free control plane) — TBD based on resource needs.

## External Dependencies

| Service | Purpose | Free Tier |
|---------|---------|-----------|
| Groq | Primary LLM provider | Free tier |
| Gemini | Backup LLM provider | Free tier |
| PostgreSQL | Memory store | Azure free 12 months (B1MS) |
| LangFuse | LLM tracing | Cloud free (50K observations/month) |
| Microsoft Teams | Notifications (incoming webhook) | Free |

## Relationship to This Repo

The current `src/sentinel/` code in this repo is the Phase 1 MVP of this backend. Phase 2 refactors it for production: swaps SQLite → PostgreSQL, adds eval suite, adds LangFuse, makes it independently deployable.

## Key Docs

| Doc | Purpose |
|-----|---------|
| `ARCHITECTURE.md` | (to be written) Full backend architecture, API contracts, deployment spec |
| `../../ARCHITECTURE.md` | Phase 1 architecture (current codebase) |
| `../../TODO.md` | Phase 1 task tracker |

## Status

Planning not started. Current Phase 1 code exists in `src/sentinel/`. Phase 2 architecture doc to be written.
