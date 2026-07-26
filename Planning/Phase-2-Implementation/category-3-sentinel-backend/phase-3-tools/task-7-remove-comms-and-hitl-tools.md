# task-7 — Remove `comms_tools.py` + `hitl.py`   ·   [category-3-sentinel-backend / phase-3-tools]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-3-tools` |
| **Commit prefix** | `refactor:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §4.8, §13.2 (remove entirely), §3.3/§7 (HITL = PR) |
| **Depends on** | [[task-6-remediation-and-escalation]] (replacement in place first) |
| **Referenced by** | tool registry, orchestrator |

## Spec
Delete the two tools that no longer exist in Phase 2: notifications are done by GHA;
HITL is the GitHub PR, not a tool. Update the tool registry + any imports.

**Files deleted:** `src/sentinel/tools/comms_tools.py`, `src/sentinel/tools/hitl.py`.
**Files modified:** tool registry / `__init__`, orchestrator wiring, tests that referenced them.

**⚠ Order:** do this only after 3.2–3.6 land, so nothing imports the removed tools mid-phase.

## Prerequisites
- [ ] tasks 3.2–3.6 done. [ ] grep confirms no remaining imports after removal.

## Acceptance Criteria
- [ ] Both files removed; no dangling imports; app boots; tool registry lists exactly the six Phase-2 tools (§3 Tools).
- [ ] No `request_human_approval` / execute path remains (safety invariant).

## Tests
- **Unit:** registry test asserts the six tools present and the two absent; `grep` clean for removed symbols.
- **Boot check:** app imports/starts.
- **Safety-reviewer:** confirm no HITL-bypass/execute tool exists.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate — end of Category 3 Phase 3)
1. `pytest -q` green; app boots.
2. `grep -r "comms_tools\|hitl\|request_human_approval" src/` → no hits.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none locally._
