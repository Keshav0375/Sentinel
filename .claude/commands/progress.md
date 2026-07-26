---
description: Show Phase-2 implementation progress from the tracker
allowed-tools: Read, Bash
model: haiku
---

Report where the Phase 2 build stands. Read, in order:

1. `Planning/Phase-2-Implementation/STATE-IMPL.md` — current category/phase/branch/PR, the
   Phase Gate Ledger, Blockers, and Open Reconciliations.
2. `Planning/Phase-2-Implementation/TODO.md` — the 58-task master checklist.

Status markers in TODO.md are **emoji cells in markdown tables**, not `[x]` checkboxes:
`⬜ not-started` · `🔵 in-progress` · `⛔ blocked` · `🟡 done-pending-review` · `✅ verified`.
A task counts as complete only at `✅`. `🔒` on a phase heading means it is locked behind an
unsigned phase gate.

Report:

- **Progress bar** for verified tasks (visual: `████░░░░░░ 40%`) — `N / 58 verified`,
  plus `P / 16 phases merged`.
- **Where we are** — active category, phase, branch, PR (or "not started").
- **This phase** — one line per task with its status emoji.
- **Next 3 actionable tasks** — skip anything in a 🔒 phase and say why it is locked.
- **Blockers** — open rows from the STATE-IMPL Blockers table, and any task marked ⛔.
- **Open Reconciliations** — any R-item still `OPEN`. These halt the task they affect, so
  call them out even when nothing is ⛔ yet.

Read-only — report, never edit the tracker.
