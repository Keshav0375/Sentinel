# task-1 — `eval/judge.py` → LangFuse scores   ·   [category-3-sentinel-backend / phase-6-eval]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-6-eval` |
| **Commit prefix** | `refactor:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §3 (judge both paths), §6.4, §13.2 |
| **Depends on** | [[task-4-langfuse-tracing]], [[task-2-orchestrator-loops]], [[task-1-provider-routing]] |
| **Referenced by** | [[task-2-eval-runner-datasets]], [[task-1-webhook-receiver]] (scores stored) |

## Spec
Judge scores trajectory quality on both rollback and escalation paths; scores pushed to
LangFuse and stored on the incident row (`eval_score` JSONB).

**Files modified:** `src/sentinel/eval/judge.py`
- Judge agent (Haiku, **different family/model discipline** from graded agents — safety invariant) scores per-dimension (triage_accuracy, root_cause_correctness, etc.).
- `push_score(trace_id, name, value)` per dimension (§6.4); return structured `JudgeResult`.
- Evaluates reasoning quality, not outcome (§3): good escalation can score well.

## Prerequisites
- [ ] Phase-4 orchestrator, tracing (2.4). [ ] fake LLM for deterministic tests.

## Acceptance Criteria
- [ ] Scores both paths; per-dimension values in [0,1]; pushed to LangFuse; stored on incident.
- [ ] Judge model distinct from the agents it grades (no self-eval).

## Tests
- **Integration (`tests/test_eval/test_judge.py`, fake LLM):** rollback + escalation trajectories both scored; scores stored; safety-reviewer confirms no self-grading.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `pytest tests/test_eval/test_judge.py -q` green.
2. (with B7) scores appear on the LangFuse trace.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live score push ⛔ B7. Judge + fake tests writable now._
