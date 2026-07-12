# Task-File Schema & Status Transitions

Every task file follows `_templates/task-template.md`. It is the spec (written up front)
AND the completion report (filled on done). Never delete the Spec when reporting — append.

## Sections
- **Header table** — Status, Repo, Local path, Phase branch, Commit prefix, Arch refs,
  Depends on, Referenced by. Keep `Depends on`/`Referenced by` as `[[task-file]]` links.
- **Spec** — files created/modified, contracts/signatures. Stable once building starts.
- **Prerequisites** — checked before work; a miss triggers the blocker protocol.
- **Acceptance Criteria** — observable, testable statements.
- **Tests** — the unit + integration tests to add and how they run under the gate.
- **How to Verify** — the steps the human runs at the phase gate to see it working.
- **Report** — filled on completion: what changed, decisions + rationale, deviations,
  test results, commit SHA(s). Write it as ground truth later tasks can cite.
- **BLOCKED** — only when halted.

## Status values (Header `Status` + TODO cell)
| Status | Meaning | TODO icon |
|--------|---------|-----------|
| `not-started` | Spec written, no work begun | ⬜ |
| `in-progress` | Actively building on the phase branch | 🔵 |
| `blocked` | Halted on a missing prerequisite (see BLOCKED) | ⛔ |
| `done-pending-review` | Built, tested, gate green, committed; awaiting phase gate | 🟡 |
| `verified` | Phase gate signed off + PR merged | ✅ |

## Transitions
```
not-started ─▶ in-progress ─▶ done-pending-review ─▶ verified
                   │
                   └─▶ blocked ─(user clears)─▶ in-progress
```
- Only `/phase-gate` may move a task (whole phase) to `verified`.
- When you flip a status, update it in THREE places consistently: the task file header,
  `TODO.md` (the status cell), and `STATE-IMPL.md` (current position / counts).

## Cross-references (the "one-liner" web the user asked for)
- Add `Referenced by` back-links when a later task depends on this one.
- In the Report, cite prior task reports as ground truth: "uses the pool from
  [[task-2-asyncpg-database-pool]]". This keeps big context navigable without re-reading
  the architecture each time.
