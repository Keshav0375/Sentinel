# task-2 — End-to-end integration smoke + eval   ·   [category-3-sentinel-backend / phase-9-cleanup]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) + full system |
| **Phase branch** | `impl/backend-phase-9-cleanup` |
| **Commit prefix** | `test:` |
| **Arch refs** | master ARCHITECTURE.md §2 (end-to-end flow); sentinel §9.3; deployment §4 |
| **Depends on** | ALL categories complete + infra/deployment verified |
| **Referenced by** | project completion |

## Spec
The final proof: a real demo failure flows through the whole system and produces a revert PR
(Condition C) and a runtime-triggered incident (Condition B), with eval scores recorded.

**Artifacts:**
- `tests/test_e2e/` — an orchestrated smoke that (offline, FAKE_LLM + local pgvector) exercises webhook → pipeline → rollback spec/escalation → judge, asserting stored incident + scores.
- A documented **live demo runbook** (`Planning/Phase-2-Implementation/DEMO.md`) for the full-stack path: dispatch demo PR #3 (C) and #11 (B), watch Datadog fire → dispatch → incident workflow → revert PR + Teams + LangFuse trace.

## Prerequisites
- [ ] All 54 prior tasks `verified`. [ ] ⛔ B1–B9 + all cloud wiring for the LIVE demo (the offline e2e smoke runs without cloud).

## Acceptance Criteria
- [ ] Offline e2e smoke green (webhook→pipeline→judge, both paths).
- [ ] Live demo runbook reproduces: Condition C → revert PR on sentinel-deployment; Condition B → runtime-health incident → revert PR; both traced in LangFuse with judge scores.
- [ ] MTTR + judge scores visible; correlation IDs traceable incident→PR→deploy→logs→trace.

## Tests
- **E2E offline:** `pytest tests/test_e2e -q` (FAKE_LLM + local DB).
- **Full-stack (⛔ B1–B9):** the DEMO.md runbook end to end.
- **Quality gate:** `--repo backend` across the whole repo.

## How to Verify (phase gate — FINAL, Category 3 Phase 9)
1. `pytest tests/test_e2e -q` green.
2. Full-stack: run the DEMO.md steps → observe a revert PR + Teams notification + LangFuse trace for both a C and a B scenario.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live full-stack demo ⛔ B1–B9 + all upstream phases verified. Offline e2e smoke writable now._
