# Task Lifecycle (authoritative)

The full loop `/sentinel-build` runs for a single task. Each numbered step maps to a SKILL
step; this file adds the detail.

## The loop

```
resolve → header → prereq ─(missing)─▶ BLOCKED + halt
                      │
                    (ok)
                      ▼
             branch (if first-in-phase)
                      ▼
      implement (spec + arch section, in-scope only)
                      ▼
        tests (unit + integration) + quality gate ──(red)──▶ fix ──┐
                      │                                             │
                    (green) ◀───────────────────────────────────────┘
                      ▼
   review: architecture-conformance (+ safety-reviewer for backend agents/tools)
                      ▼
             commit (one task = one commit, no attribution)
                      ▼
      report + set done-pending-review + update TODO/STATE-IMPL
                      ▼
        last task in phase? ──yes──▶ tell user to run /phase-gate
                      │
                     no ──▶ next task (same phase branch)
```

## Definition of done for a task
A task is `done-pending-review` only when ALL hold:
- Every file in the Spec exists and matches; nothing out of scope added.
- Every Acceptance Criterion is checked.
- Unit + integration tests exist and pass.
- `quality_gate.py --repo <name>` is green (no required check failing).
- `architecture-conformance` returns `CONFORMS` (blockers resolved).
- One commit made with a conventional message, no Claude attribution.
- Task file Report/Tests/How-to-Verify filled; TODO + STATE-IMPL updated.

A task is `verified` only after its **phase** passes `/phase-gate` and the PR merges.

## Cross-repo note
Actual code for infra/deployment lands in their own repos (separate working dirs). The
tracking docs in `Planning/Phase-2-Implementation/` live in the `Sentinel` repo — update
them there. Keep the two in step: a task isn't done until both the code (target repo) and
the report (tracking) are written.

## Scope discipline
- Build only what the current task specifies. Resist "while I'm here" edits — they belong to
  their own task and break the one-commit-per-task and one-PR-per-phase model.
- If implementing reveals the architecture is wrong or under-specified, STOP: surface it,
  and if it's a design change route it through `/sentinel-planner` (update the arch doc +
  STATE) before continuing. Do not silently diverge.
