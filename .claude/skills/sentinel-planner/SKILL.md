---
name: sentinel-planner
description: "Sentinel Phase 2 planning agent. Manages architecture docs, TODOs, READMEs, reference links, and planning state across all repos (sentinel, sentinel-deployment, sentinel-infra). Trigger: /sentinel-planner"
trigger: /sentinel-planner
---

# /sentinel-planner

Sentinel planning agent — keeps architecture, TODOs, READMEs, references, and planning state in sync across the Phase 2 multi-repo system.

## Usage

```
/sentinel-planner                          # Resume planning — reads state, suggests next step
/sentinel-planner status                   # Show current planning state across all repos
/sentinel-planner decide <topic>           # Start a decision discussion, update docs after approval
/sentinel-planner update                   # Sync all READMEs and state file after a discussion
/sentinel-planner review <repo>            # Review completeness of a specific repo's planning docs
```

## What You Must Do When Invoked

### Step 0 — Load instructions

Read the following files in this order. They contain the domain rules this agent operates under.

1. `.claude/skills/sentinel-planner/instructions/state-management.md` — how to read/write planning state
2. `.claude/skills/sentinel-planner/instructions/doc-patterns.md` — how each doc type works (architecture, TODO, README, etc.)
3. `.claude/skills/sentinel-planner/instructions/decision-flow.md` — how to run a planning decision
4. `.claude/skills/sentinel-planner/instructions/resources.md` — what resources/budgets are available

### Step 1 — Read current state

Read `Planning/Phase-2/STATE.md` to understand where planning currently stands. This file is the single source of truth for planning progress.

### Step 2 — Read repo READMEs

Read the README.md in each repo folder to understand current state per repo:
- `Planning/Phase-2/sentinel/README.md`
- `Planning/Phase-2/sentinel-deployment/README.md`
- `Planning/Phase-2/sentinel-infra/README.md`

### Step 3 — Read context as needed

Depending on the task, also read:
- `Planning/Phase-2/reference-documentation/links.md` — external docs and findings
- `Planning/Subscription-plans/azure-reference.md` — Azure free tier details
- `Planning/Subscription-plans/github-pro-reference.md` — GitHub/Datadog/DO credits
- `Planning/LLM-providers.md/available_models.md` — LLM provider capabilities
- `Planning/MVP/ARCHITECTURE.md` — Phase 1 architecture (for migration context)
- `Planning/MVP/TODO.md` — Phase 1 tasks (for what's already built)
- Any existing `ARCHITECTURE.md` in the target repo folder

### Step 4 — Execute the requested action

Based on the subcommand (or lack thereof), follow the appropriate instruction file:

| Subcommand | Instruction File |
|------------|-----------------|
| _(none)_ | `decision-flow.md` — resume planning, suggest next step |
| `status` | `state-management.md` — read and report state |
| `decide <topic>` | `decision-flow.md` — start a decision |
| `update` | `state-management.md` + `doc-patterns.md` — sync all docs |
| `review <repo>` | `doc-patterns.md` — check completeness |

### Step 5 — Update state and docs

After ANY planning discussion (not just when `/sentinel-planner update` is called):

1. Update `Planning/Phase-2/STATE.md` with new decisions, progress, blockers
2. Update the relevant repo's `README.md` with new status/findings
3. If architecture decisions were made, update or create the repo's `ARCHITECTURE.md`
4. If new external docs were referenced, add them to `reference-documentation/links.md`
5. If implementation tasks were identified, note them (but don't create TODOs until architecture is finalized)

**Do NOT ask for permission to update docs.** After the user approves a decision or new finding, update all relevant files automatically. The user expects docs to stay current without being asked "should I update X?".
