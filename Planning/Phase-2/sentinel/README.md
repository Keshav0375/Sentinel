# sentinel (this repo)

Full knowledge base for the Sentinel system — the autonomous DevOps incident response agent.

This is the **main repo** (the one you're reading this from). Phase 2 transforms it from an MVP portfolio demo into a production-grade system with real infrastructure, real deployments, and real incident data flowing through it.

## What Lives Here

- GHA workflows that **are** the orchestration layer (event-driven via `repository_dispatch`)
- The intelligent backend (agent orchestration, memory, eval) — deployed separately but defined here
- All planning docs, architecture decisions, and reference material

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| CI pipeline | 4 linear jobs (Setup → Branch → Tests → Summary) | Clean GHA UI, blocks merge on failure |
| CI trigger | PR to main only | No wasted runs on feature branch pushes |
| LLM providers | Groq primary, Gemini backup | Free tier, OpenAI-compatible |
| Notification channel | Microsoft Teams | Incoming webhook, no Slack |
| Event routing | Event Grid → Azure Functions → repository_dispatch | All Azure always-free tier |

## Phase 2 Scope

- [ ] Event-driven GHA orchestration (Azure Event Grid → Azure Function → `repository_dispatch`)
- [ ] Multi-agent pipeline: Triage → Analysis → Resolution → Judge
- [ ] Episodic + semantic memory backed by PostgreSQL
- [ ] HITL safety gates for destructive actions
- [ ] LangFuse integration for LLM tracing
- [ ] Trajectory-level eval suite
- [ ] Microsoft Teams notifications

## Key Docs

| Doc | Purpose |
|-----|---------|
| `ARCHITECTURE.md` (root) | Phase 1 architecture (current) |
| `Planning/Phase-2/sentinel/` | Phase 2 planning (this folder) |
| `Planning/Phase-2/reference-documentation/links.md` | External service docs and findings |

## Status

Planning in progress. Architecture docs being written.
