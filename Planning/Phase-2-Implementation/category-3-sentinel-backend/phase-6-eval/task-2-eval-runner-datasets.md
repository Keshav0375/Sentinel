# task-2 — `eval/runner.py` → LangFuse datasets   ·   [category-3-sentinel-backend / phase-6-eval]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `impl/backend-phase-6-eval` |
| **Commit prefix** | `refactor:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §6.1 (datasets), §13.2 |
| **Depends on** | [[task-1-judge-langfuse-scores]] |
| **Referenced by** | eval dashboard / regression testing |

## Spec
Rewrite the eval runner to work against LangFuse datasets (was iterating `data/` scenarios,
which are deleted in Phase 9) — run new prompt versions against historical trajectories.

**Files modified:** `src/sentinel/eval/runner.py`
- Pull a LangFuse dataset of past trajectories; re-run the pipeline (or judge) against each; aggregate scores; optional `GET /eval/results` data source (§1.1 route).
- No dependency on `data/scenarios/`.

## Prerequisites
- [ ] task 6.1 judge. [ ] ⛔ B7 LangFuse for real datasets (offline: a local fixture dataset for tests).

## Acceptance Criteria
- [ ] Runner iterates a dataset (fixture offline) and produces aggregate scores; no `data/` reference.
- [ ] Feeds the eval results endpoint.

## Tests
- **Integration (`tests/test_eval/test_runner.py`, fixture dataset + fake LLM):** runs, aggregates, returns a report.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate — end of Category 3 Phase 6)
1. `pytest tests/test_eval/test_runner.py -q` green.
2. (with B7) run against a real LangFuse dataset → aggregate scores.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Real datasets ⛔ B7. Runner + fixture tests writable now._
