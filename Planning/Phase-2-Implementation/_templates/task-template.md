# task-{K} — {title}   ·   [category-{N}-{repo} / phase-{M}-{slug}]

<!-- One PR-sized unit of work. This file is BOTH the spec (written up front) and the
     completion report (filled in when the task is done). Keep the spec stable; append
     to Report/Tests as you work. Cross-link sibling tasks with [[task-file-name]]. -->

| Field | Value |
|-------|-------|
| **Status** | `not-started` \| `in-progress` \| `blocked` \| `done-pending-review` \| `verified` |
| **Repo** | `Sentinel-infra` \| `Sentinel-deployment` \| `Sentinel` (backend) |
| **Local path** | `Agentic-Engineering/Sentinel-development-project/<repo>` (backend = `Agentic-Engineering/Sentinel`) |
| **Phase branch** | `impl/<cat>-phase-<M>-<slug>` (this task is a commit on the phase branch) |
| **Commit prefix** | `feat:` \| `fix:` \| `refactor:` \| `test:` \| `docs:` |
| **Arch refs** | `Planning/Phase-2/<repo>/ARCHITECTURE.md §X.Y` |
| **Depends on** | [[task-file]] … (must be `verified` or same-phase `done-pending-review`) |
| **Referenced by** | [[task-file]] … |

## Spec
<!-- What to build. Concrete: files to create/modify, function/resource signatures,
     contracts, config keys. Reference the architecture section; don't duplicate it wholesale. -->

**Files created:**
- `path/to/file` — purpose

**Files modified:**
- `path/to/file` — change

**Contract / signatures:**
```text
（key types, resource names, endpoint shapes, tool I/O）
```

## Prerequisites
<!-- Checked BEFORE work starts. Tools that must be installed, accounts/keys that must
     exist, upstream tasks that must be verified. Missing → fill BLOCKED, halt. -->
- [ ] Tooling: …
- [ ] Accounts/secrets: …
- [ ] Upstream tasks verified: …

## Acceptance Criteria
<!-- What "done" looks like — observable, testable statements. -->
- [ ] …

## Tests
<!-- Unit + integration to add, and how they run under the category quality gate. -->
- **Unit:** …
- **Integration:** …
- **Quality gate:** `python scripts/quality_gate.py --repo <name>` (lint · types/validate · secrets · tests)

## How to Verify (for the phase gate)
<!-- Steps the human runs to see this feature actually working. Filled/confirmed at completion. -->
1. …

## Report   ·   _filled on completion_
<!-- What actually changed, decisions made + rationale, deviations from spec, test results,
     commit SHA(s). Written so later tasks can reference this as ground truth. -->
_not yet implemented_

## BLOCKED   ·   _only if halted_
<!-- What is missing, exact error/condition, who must resolve, what it blocks downstream.
     Mirror into STATE-IMPL.md Blockers. -->
_none_
