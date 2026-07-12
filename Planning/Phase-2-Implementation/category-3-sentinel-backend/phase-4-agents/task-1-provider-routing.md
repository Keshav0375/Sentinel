# task-1 — Provider routing + fallback (`providers/`)   ·   [category-3-sentinel-backend / phase-4-agents]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `impl/backend-phase-4-agents` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §4.7 (models + fallback), §1.3 (providers/ bucket) |
| **Depends on** | [[task-1-config-extensions]] |
| **Referenced by** | [[task-2-orchestrator-loops]], [[task-4-pr-content-generator]], all agents |

## Spec
LLM provider routing: Anthropic default, OpenAI fallback, per-agent model assignment, and
automatic fallback on rate-limit/timeout/API error (§4.7).

**Files created:** `src/sentinel/providers/` — `__init__.py`, `router.py`, maybe `models.py`.
- Model map per agent/step (§4.7 table): orchestrator/analysis/resolution → `anthropic/claude-sonnet-4-6` (fb `openai/gpt-4o`); triage/reflexion/reverification/judge/pr-content → `anthropic/claude-haiku-4-5` (fb `openai/gpt-4o-mini`).
- `call_with_fallback(agent, input, primary, fallback)` per §4.7 snippet; switch default via `sentinel_primary_provider`.
- `SENTINEL_FAKE_LLM=1` → a deterministic stub provider for CI (no network).
- Uses OpenAI Agents SDK `Agent.with_model(...)` pattern.

## Prerequisites
- [ ] task 3.1 config. [ ] ⛔ B4/B5 for live calls (fake provider for tests).

## Acceptance Criteria
- [ ] Correct model string per agent role (conformance-checked against §4.7).
- [ ] Fallback triggers on RateLimit/Timeout/APIError and logs the switch (structlog).
- [ ] Fake mode returns canned outputs, no network.

## Tests
- **Unit (`tests/test_providers/test_router.py`):** primary success path; forced primary error → fallback invoked; model map matches §4.7; fake mode deterministic.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `pytest tests/test_providers/ -q` green (fake + fallback).
2. (with B4/B5) a real Haiku call returns a completion.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live calls ⛔ B4/B5. Router + fake-mode tests writable now._
