# task-4 — `infra/tracing.py` → LangFuse (observe + prompts + scoring)   ·   [category-3-sentinel-backend / phase-2-memory-and-tracing]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-2-memory-and-tracing` |
| **Commit prefix** | `refactor:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §6 (all), §13.2 |
| **Depends on** | [[task-1-phase2-dependencies]], [[task-1-config-extensions]] (keys) — note: config is Phase 3; use env directly here or land config keys first |
| **Referenced by** | [[task-2-orchestrator-loops]], [[task-1-judge-langfuse-scores]], all agents |

## Spec
Replace custom trace capture with LangFuse: `@observe` decorators, LangFuse-first prompt
loading with local fallback, and score piping (§6.2–6.4).

**Files modified:** `src/sentinel/infra/tracing.py`
- Init `Langfuse()` from env (public/secret/host).
- `@observe` wrappers (or helpers) for pipeline / agent / tool spans; `langfuse_context.update_current_trace(metadata, tags)`.
- `async def load_prompt(agent_name)` — LangFuse `get_prompt(cache_ttl=300)` with fallback to `agents/prompts/{name}.txt` (§6.3).
- `push_score(trace_id, name, value)` for judge dimensions (§6.4).
- Degrade gracefully when LangFuse is unreachable (fallback to file prompts, no crash).

## Prerequisites
- [ ] task 1.1 deps. [ ] ⛔ B7 LangFuse keys for live tracing (fallback path works offline).

## Acceptance Criteria
- [ ] Prompt loader returns LangFuse prompt when available, file otherwise (test both).
- [ ] Trace/score helpers callable and typed; no hard failure when keys absent.

## Tests
- **Unit (`tests/test_infra/test_tracing.py`):** prompt fallback to file when LangFuse mocked-unavailable; score helper builds the right call (mock client).
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate — end of Category 3 Phase 2)
1. `pytest tests/test_infra/test_tracing.py -q` green (fallback path).
2. (with B7 keys) a sample `@observe` run appears as a trace in the LangFuse dashboard.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live tracing ⛔ B7. Fallback + unit tests writable now._
