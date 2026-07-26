---
name: implement-phase
description: "Sentinel Phase 2 build orchestrator. Implements ONE whole phase end-to-end — identifies where we are, rebuilds full context (summarizing prior work), locks onto the architecture, then iterates task-by-task to a green phase, handing off to specialist subagents for context, architecture, code, and safety. Closes by asking the user to verify the phase works. Trigger: /implement-phase"
trigger: /implement-phase
---

# /implement-phase

The single build-time driver for Sentinel Phase 2. It owns **one phase at a time** and
reaches the phase goal through **iteration + handoffs** to a small team of specialist
subagents. You (the orchestrator) plan, sequence, edit code, and run gates; the subagents
each do one thing well and hand their result back to you.

> **This replaces `/sentinel-build` + `/phase-gate` + `/sentinel-planner`.** There is no
> separate task command and no separate gate command — a phase is planned, built, reviewed,
> and signed off inside this one loop.

## Core principles (do not violate)

- **Rebuild full context every run.** Never assume memory of prior phases. Summarize what's
  already done from the tracker + git, so each invocation starts from ground truth.
- **Architecture is law.** Everything you build conforms to `ARCHITECTURE.md` for the active
  repo. If the spec and the architecture disagree, **halt and ask** — never guess.
- **Iterate to the goal.** Track the phase's tasks as a live goal list (TodoWrite). Loop each
  task until its quality gate is green before moving on. The phase is done only when *all*
  its tasks are green.
- **Handoffs, not heroics.** Delegate context, architecture distillation, and review to the
  specialist subagents (below). You integrate their outputs; you don't re-do their jobs.
- **Ask when unsure. Halt when blocked.** Ambiguity in a task or architecture → stop and ask.
  Missing prerequisite (tool, key, upstream task not verified) → write the blocker down, stop,
  do not partially build.
- **No Claude attribution — ever.** No `Co-Authored-By: Claude`, no "Generated with Claude
  Code" in any commit or PR, in any of the three repos. The user is sole author. (See
  CLAUDE.md Git Rule.)

## The subagent team (handoff targets)

| Subagent | Role | You call it… |
|----------|------|--------------|
| `phase-context-builder` | The **contexter/summarizer** — reads the phase folder + STATE-IMPL history + git log, returns a compact "where we are" brief summarizing prior completed work and this phase's shape. | **Step 1**, at the start of every run. |
| `architecture-warden` | The **architect** — `mode: distill` extracts the binding architecture constraints for this phase; `mode: review` checks the finished diff against spec + architecture. | **Step 2** (distill) and **Step 5** (review). |
| `code-reviewer` | The **code reviewer** — correctness, coding standards, style-match on the phase diff. | **Step 5**. |
| `safety-reviewer` | The **safety reviewer** — HITL gates, tool-call caps, destructive-action guards. | **Step 5**, for backend phases (or any diff touching tools/agents/orchestrator). |

Repo locations (you already know these — do not ask):
- **infra** → `../Sentinel-development-project/Sentinel-infra`
- **deployment** → `../Sentinel-development-project/Sentinel-deployment`
- **backend** → `.` (this repo, `Keshav0375/Sentinel`)

## Branch model (all three repos)

```
release-phase-2  ──┬──►  dev/<cat>-phase-<M>-<slug>  ──PR──►  release-phase-2
                   │            (one branch + one PR per phase)
                   └──────────────────────────────────────────►  main
                            (final, once ALL phases are merged)
```

- **Every phase branches from an up-to-date `release-phase-2`**, never from `main`.
- Branch prefix is **`dev/`** (not `impl/`) — `ci.yml`'s branch-convention check only
  accepts the `dev|feat|fix|refactor|ci|docs|test|chore|planning|ai|hotfix` prefixes.
- The phase PR targets **`release-phase-2`**. `guard-main-source.yml` enforces
  `release-phase-2 <- dev/* | planning/phase-2-e2e` and `main <- release-phase-2`.
  **A PR from `dev/*` straight to `main` will be rejected** — never open one.
- `planning/phase-2-e2e` carries plans, architecture, and the tracker. `release-phase-2`
  → `main` happens once, at the end of Phase 2, after the planning docs are removed.
- **Tracker commits ride the phase's `dev/` branch in this (backend) repo.** TODO.md,
  STATE-IMPL.md and the task files live here, so an **infra or deployment** phase produces
  **two branches and two PRs**, closed together at the same gate:
  | | code | tracker |
  |---|---|---|
  | infra / deployment phase | `dev/<cat>-phase-<M>-<slug>` in the sibling repo | `dev/<cat>-phase-<M>-<slug>` in `Sentinel` (docs only) |
  | backend phase | one branch — code and tracker are both in `Sentinel` | — |

---

## Usage

```
/implement-phase              # Build the current active phase to completion
/implement-phase <cat-phase>  # Build a specific phase, e.g. infra-2 or backend-4
/implement-phase status       # Report where we are + the phase's task states (no changes)
/implement-phase resume       # Resume the active phase from its last green task
```

---

## Step 0 — Locate ourselves

Read, in order:
1. `Planning/Phase-2-Implementation/STATE-IMPL.md` — current category/phase/branch/PR, blockers, ledger.
2. `Planning/Phase-2-Implementation/TODO.md` — the 58-task master checklist + phase-gate locks.
3. The active category's `README.md` under `Planning/Phase-2-Implementation/category-*/`.

Resolve the target phase:
- No arg → the first phase (category order infra → deployment → backend; then phase order)
  that has any `not-started`/`in-progress` task **and** whose predecessor phase is signed off.
- **Never enter a phase whose predecessor gate is unsigned.** If the previous phase is only
  `done-pending-review`, stop and tell the user to complete Step 6 sign-off on it first.
- Explicit `<cat-phase>` → that phase, but refuse (and explain) if it would skip a locked phase.

## Step 1 — Rebuild context (handoff → `phase-context-builder`)

Dispatch `phase-context-builder` with the active category + phase. It returns a brief:
what prior phases delivered (summarized, not dumped), this phase's tasks and their specs in
one place, upstream dependencies + their status, and any standing blockers from STATE-IMPL.
**Do this every run** — it is how we start from ground truth instead of stale memory.

## Step 2 — Lock onto the architecture (handoff → `architecture-warden` · `mode: distill`)

Dispatch `architecture-warden` in **distill** mode for this phase. It returns the exact
architecture sections, contracts (resource/env/endpoint/table/model names, tool I/O types,
Pydantic fields), and binding decisions this phase must honor. Keep this brief open — it is
your conformance contract for the whole phase.

If the context brief or the architecture reveals ambiguity or a spec/arch conflict → **halt
and ask the user** before writing any code.

## Step 3 — Set the goal (TodoWrite) + branch

- Write one todo per task in the phase (this is the "goal" you iterate toward). Mark the first
  `in_progress`.
- Print a compact phase header so the user always sees the plan:
  ```
  ▶ <category> · phase <M> (<slug>)  —  <N> tasks
    goal:   <one line: what this phase delivers>
    repo:   <repo> (<local path>)
    branch: dev/<cat>-phase-<M>-<slug>
    tasks:  <K.1 …> · <K.2 …> · …
  ```
- Create the phase branch off a fresh `release-phase-2` (see Branch model above):
  ```
  git -C <repo> checkout release-phase-2 && git -C <repo> pull
  git -C <repo> checkout -b dev/<cat>-phase-<M>-<slug>
  ```
  If `release-phase-2` does not exist in the target repo, **stop and ask** — do not
  branch from `main` as a fallback and do not create the integration branch yourself.
- For an **infra or deployment** phase, also create the same-named tracker branch in this
  repo: `git checkout release-phase-2 && git pull && git checkout -b dev/<cat>-phase-<M>-<slug>`.
  All TODO/STATE-IMPL/task-file updates for the phase commit there.

## Step 4 — Iterate the phase, task by task

For each task in order, run this loop (the "keep-checking" loop):

1. **Prerequisite check** — tools present, keys present (per STATE-IMPL blockers), upstream
   tasks `verified`, **and every STATE-IMPL "Open Reconciliation" (R-item) that names this
   task or its phase is RESOLVED**. An open R-item is a decision the user owes you — a value
   you would otherwise silently invent (region, owner, repo name). If any is missing →
   **halt**: write a BLOCKED note into the task file, mirror it to STATE-IMPL Blockers, set
   the task `blocked`, and STOP. Do not partially build, and never default an open R-item.
2. **Implement** strictly to the task Spec + the `architecture-warden` contract. Match the
   surrounding code's style. Do not exceed scope. Ambiguity or arch conflict → **halt and ask.**
3. **Test** — add the unit + integration tests the task names.
4. **Quality gate** — `python scripts/quality_gate.py --repo <infra|deployment|backend> --path <repo>`.
   Red → fix and re-run. **Loop until green.** Green is mandatory before commit.
   - `RESULT: INCONCLUSIVE` is **not** green — nothing ran. Say so and stop; don't commit.
   - `PASS` with a `NOT verified (skipped)` line is a **partial** pass. Report exactly which
     checks were skipped and why; never present it as a clean gate.
   - If the task added a `tests/` package that isn't in `quality_gate.py`'s `MATRIX`, its tests
     never ran — add the path to `MATRIX` in the same commit.
5. **Commit** — one task = one commit, conventional prefix (`feat|fix|refactor|test|docs`).
   **No Claude attribution.** Author is the user.
6. **Record** — fill the task file's Report / Tests / How to Verify, set status
   `done-pending-review`, update its TODO.md cell, mark its todo `completed`, advance to the next.

Never skip step 1 or step 4.

## Step 5 — Review the finished phase (handoffs → reviewers)

When every task is `done-pending-review` + green, run the review team over the whole phase diff:

1. `architecture-warden` · `mode: review` — spec + architecture conformance.
2. `code-reviewer` — correctness + standards.
3. `safety-reviewer` — **only** for backend phases or any diff touching tools/agents/orchestrator.

Resolve every 🚨 BLOCKER before proceeding (loop back into Step 4 for fixes on the same
branch). ⚠️ findings: fix or consciously note. Improvise sensibly — if a reviewer surfaces a
real gap the spec missed, raise it with the user rather than silently expanding scope.

## Step 6 — Close the phase (human verification — the gate)

1. Push the branch and open the PR **to `release-phase-2`** (never to `main`) in the target
   repo with `gh pr create --base release-phase-2`:
   - Title: `<cat> phase <M> — <phase name>`.
   - Body: one bullet per task (with commit subject) + the aggregated **How to Verify** steps.
     **No Claude attribution in the body.** If `gh`/remote is unavailable, say so and give the
     manual push/PR commands — never fabricate a PR URL.
   - For an infra/deployment phase, open the **tracker PR** in this repo too (same title +
     ` (tracker)`), also based on `release-phase-2`, and link the two in each body.
2. Present the **"see it working" checklist** — concise and concrete: what the phase delivered
   (one line per task) and the exact commands/URLs/UI steps to confirm it, ordered top-to-bottom.
   Be honest about anything deferred/BLOCKED (e.g. infra that needs a live Azure account).
3. **Ask the user to verify** with `AskUserQuestion` — "Does <phase> work as expected?":
   - **Approve & merge** — verified → merge **into `release-phase-2`** (`gh pr merge --squash
     --delete-branch` unless they prefer a merge commit; merge the code PR first, then the
     tracker PR), mark every task `verified` (task files + TODO cells ✅), append a row to the
     Phase Gate Ledger in STATE-IMPL, unlock the next phase (drop its 🔒), state what's next.
     **Never merge a phase into `main`** — `release-phase-2` → `main` is a single, separate
     merge at the end of Phase 2, after the planning docs are removed, and the user drives it.
   - **Changes needed** — record their feedback into the relevant task(s) (`in_progress`) +
     STATE-IMPL, do NOT merge, loop back to Step 4; the same PR updates as fix commits land.
   - **Hold** — leave the PR open, don't merge.

**Never** merge without an explicit Approve. **Never** mark a phase `verified` the user hasn't
confirmed with their own eyes. **Never** add Claude as a contributor to any commit, PR, or
merge commit.
