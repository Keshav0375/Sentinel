---
name: sentinel-build
description: "Sentinel Phase 2 implementation driver. Builds the next PR-sized task end-to-end — prereq check, branch, implement against architecture, unit+integration tests, quality gate, review, commit, report, update tracker. Trigger: /sentinel-build"
trigger: /sentinel-build
---

# /sentinel-build

Execution counterpart to `/sentinel-planner`. Planner owns design-time docs; this owns
building them. It drives **one task at a time** through the loop, respecting category
order (infra → deployment → backend), phase-gate locks, and the branch-per-phase git model.

## Usage

```
/sentinel-build                 # Build the next unblocked task
/sentinel-build <task-id>       # Build a specific task, e.g. infra-2.1 or backend-3.4
/sentinel-build status          # Report position, current branch/PR, blockers (no changes)
/sentinel-build resume          # Resume an in-progress task from its last state
```

## Step 0 — Load instructions

Read, in order:
1. `.claude/skills/sentinel-build/instructions/task-lifecycle.md` — the authoritative per-task loop
2. `.claude/skills/sentinel-build/instructions/task-schema.md` — task-file format + status transitions
3. `.claude/skills/sentinel-build/instructions/git-model.md` — branch-per-phase, commit-per-task, no attribution
4. `.claude/skills/sentinel-build/instructions/quality-gate.md` — category-aware checks + tests
5. `.claude/skills/sentinel-build/instructions/blocker-protocol.md` — how to halt on a missing prerequisite

## Step 1 — Read state

- `Planning/Phase-2-Implementation/STATE-IMPL.md` — current category/phase/branch/PR, blockers, reconciliations.
- `Planning/Phase-2-Implementation/TODO.md` — the master checklist.
- The active category's `README.md`.

## Step 2 — Resolve the target task

- No arg → the first task that is `not-started`/`in-progress`, honoring: category order,
  intra-phase task order, and **phase-gate locks** (never enter a phase whose predecessor
  gate is not signed off — if the previous phase is only `done-pending-review`, stop and
  tell the user to run `/phase-gate`).
- Explicit id → that task, but refuse (and explain) if it would skip a locked phase.

## Step 3 — Print the chat header

Emit a compact summary so the user always knows what's happening:

```
▶ <category> · phase <M> (<slug>) · task <K> — <title>
  goal:    <one line>
  repo:    <repo> (<local path>)
  branch:  impl/<cat>-phase-<M>-<slug>
  arch:    <ARCHITECTURE.md §refs>
  files:   <create/modify list>
  deps:    <upstream task ids + their status>
```

## Step 4 — Prerequisite check

Run the task's Prerequisites. If any is missing (tool absent, account/key not present per
STATE-IMPL blockers, upstream task not `verified`), follow **blocker-protocol.md**: write a
BLOCKED section into the task file, mirror it to STATE-IMPL Blockers, set status `blocked`,
and STOP. Do not partially build.

## Step 5 — Branch (git-model.md)

If this task is the FIRST of its phase: `git -C <repo> checkout main && git pull` then
`git checkout -b impl/<cat>-phase-<M>-<slug>`. Otherwise stay on the existing phase branch.

## Step 6 — Implement

Build strictly to the task **Spec** + the cited architecture section. Match the surrounding
code's style. Do not exceed scope; if the spec is ambiguous or conflicts with architecture,
STOP and ask (per CLAUDE.local.md).

## Step 7 — Tests + quality gate

Add the unit + integration tests named in the task. Run
`python scripts/quality_gate.py --repo <infra|deployment|backend> --path <repo>`.
Fix failures. Green gate is mandatory before commit.

## Step 8 — Review

Dispatch the `architecture-conformance` subagent (spec match) and, for backend changes to
tools/agents/orchestrator, `safety-reviewer` (Phase-2 invariants in quality-gate.md).
Resolve any 🚨 findings before committing.

## Step 9 — Commit (one task = one commit)

Conventional prefix (`feat|fix|refactor|test|docs`). **No Claude attribution** — no
`Co-Authored-By`, no "generated with". Author is the user.

## Step 10 — Report + update tracker

Fill the task file's **Report**, **Tests**, **How to Verify**. Set status
`done-pending-review`. Update TODO.md status cell + STATE-IMPL current position.

## Step 11 — Phase end

If that was the last task in the phase and all are `done-pending-review` + green, tell the
user the phase is ready and instruct them to run `/phase-gate` (do NOT open the PR or merge
yourself — that is the gate's job and requires human verification).

**Never** skip Steps 4, 7, 8, or 11. The gate and the blocker halt are the safety of this system.
