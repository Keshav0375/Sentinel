---
name: phase-gate
description: "Human-in-the-loop checkpoint that closes a Sentinel Phase 2 phase — verifies all tasks are green, opens the phase PR, presents a 'see it working' checklist, and on user sign-off merges to main and unlocks the next phase. Trigger: /phase-gate"
trigger: /phase-gate
---

# /phase-gate

The human-review gate between phases. A phase's code does not merge and the next phase does
not start until the user confirms — with their own eyes — that the feature works. This
mirrors Sentinel's own HITL philosophy at the planning level.

## Usage
```
/phase-gate                 # Gate the current active phase (from STATE-IMPL)
/phase-gate <cat-phase>     # Gate a specific phase, e.g. infra-1 or backend-3
/phase-gate ledger          # Show the phase-gate ledger (no changes)
```

## Step 0 — Preconditions (refuse if unmet)
Read `Planning/Phase-2-Implementation/STATE-IMPL.md` + `TODO.md` + every task file in the
phase. Refuse to gate unless ALL of the phase's tasks are `done-pending-review` and their
quality gates were green. If any task is `blocked`/`in-progress`, say which, and stop.

## Step 1 — Re-run the gate on the whole phase
From the phase branch, run `python scripts/quality_gate.py --repo <name> --path <repo>`
once more over the full change set. Red → report and stop (route back to `/sentinel-build`).

## Step 2 — Open the phase PR
In the target repo, push the phase branch and open the PR to `main`:
- Title: `<cat> phase <M> — <phase name>`.
- Body: bullet the tasks (with their commit subjects) + the aggregated **How to Verify**
  steps pulled from each task file. **No Claude attribution** in the body.
- Use `gh pr create`. If `gh` or the remote is unavailable, say so and provide the manual
  push/PR commands — do not fabricate a PR URL.

## Step 3 — Present the verification checklist
Summarize for the user, concise and concrete:
- What this phase delivered (one line per task).
- **How to see it working** — the exact commands/URLs/UI steps (from each task's How to
  Verify), ordered so the user can run them top to bottom.
- Anything deferred/BLOCKED (e.g. infra pieces that need Azure to fully verify) — be honest
  about what they can and cannot confirm yet.

## Step 4 — Ask for sign-off
Use `AskUserQuestion`: "Does <phase> work as expected?" with options:
- **Approve & merge** — feature verified, merge to main, unlock next phase.
- **Changes needed** — something's off; keep the phase open (they'll describe what).
- **Hold** — leave the PR open, don't merge yet.

## Step 5a — On approve
- Merge the PR (`gh pr merge --squash --delete-branch` unless the user prefers merge-commit).
- Mark every task in the phase `verified` (task files + TODO.md cells ✅).
- Append a row to the **Phase Gate Ledger** in STATE-IMPL (date, category, phase, branch,
  PR, "verified by: Keshav", notes) and update current position + counts.
- Unlock the next phase (remove its 🔒 in TODO) and state what's next.

## Step 5b — On changes needed / hold
- Record the user's feedback in the phase's tasks (reopen the relevant task → `in-progress`,
  add a note) and in STATE-IMPL. Do NOT merge. Hand back to `/sentinel-build` for fixes;
  the same PR updates when the fix commits land on the branch.

## Never
- Never merge without an explicit Approve.
- Never mark a phase `verified` the user hasn't confirmed.
- Never add Claude as a contributor to the PR or merge commit.
