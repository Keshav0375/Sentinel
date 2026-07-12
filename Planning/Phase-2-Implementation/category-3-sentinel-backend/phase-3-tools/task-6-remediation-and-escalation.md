# task-6 — `prepare_rollback_spec` + `format_escalation`   ·   [category-3-sentinel-backend / phase-3-tools]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `impl/backend-phase-3-tools` |
| **Commit prefix** | `refactor:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §3 Tools, §4.8, §13.2; HITL §3.3/§7 |
| **Depends on** | [[task-1-config-extensions]] |
| **Referenced by** | resolution agent, [[task-1-webhook-receiver]] (response), [[task-3-generate-pr-content-endpoint]] |

## Spec
Replace Phase-1 `draft_rollback_pr` (mock PR payload) with **output-only** tools — the
backend never calls GitHub; GHA creates the PR (§3.3 "backend reasons, GHA executes").

**Files modified:** `src/sentinel/tools/remediation_tools.py`
- `prepare_rollback_spec(...)` → `RollbackSpec{target_commit_sha, justification, evidence_summary, confidence}`. **No external calls.**
- `format_escalation(...)` → `EscalationContext{hypothesis, evidence, confidence, missing}` for Teams.
- Both `@function_tool`, typed, pure (no side effects), structured errors.

## Prerequisites
- [ ] task 3.1 config.

## Acceptance Criteria
- [ ] Neither tool performs I/O to GitHub/Teams (safety invariant: no tool touches external state).
- [ ] Outputs match the shapes consumed by the webhook response + `/generate/pr-content`.

## Tests
- **Unit (`tests/test_tools/test_remediation_tools.py`):** given root cause + target deploy, produces a well-formed spec; escalation formats full context; asserts NO network calls.
- **Safety-reviewer:** run it — confirm output-only, no execute path.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `pytest tests/test_tools/test_remediation_tools.py -q` green.
2. safety-reviewer reports no external-state access.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none locally._
