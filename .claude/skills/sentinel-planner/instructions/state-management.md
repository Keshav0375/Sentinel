# State Management

## STATE.md — The Planning State File

`Planning/Phase-2/STATE.md` is the single source of truth for where planning stands. It tracks:

1. **Current phase** — which stage of planning we're in
2. **Per-repo status** — what's decided, what's pending, what's blocked
3. **Open decisions** — things that need to be resolved before moving forward
4. **Blockers** — external dependencies (accounts, credentials, manual setup)
5. **Recent decisions** — log of what was decided and when (most recent first)

### Format

```markdown
# Sentinel Phase 2 — Planning State

> Last updated: YYYY-MM-DD

## Current Phase

<one-line description of where we are>

## Repo Status

| Repo | Architecture | TODO | Testing | README | Status |
|------|-------------|------|---------|--------|--------|
| sentinel | ... | ... | ... | ... | ... |
| sentinel-deployment | ... | ... | ... | ... | ... |
| sentinel-infra | ... | ... | ... | ... | ... |
| sentinel-backend | ... | ... | ... | ... | ... |

Status values: not-started | in-progress | draft | final | blocked

## Open Decisions

- [ ] <decision needed> — context: <why it matters>
- [ ] <decision needed> — context: <why it matters>

## Blockers

- [ ] <blocker> — who: <who needs to act> — impact: <what it blocks>

## Decision Log

### YYYY-MM-DD: <decision title>
**Context:** <why this came up>
**Decision:** <what was decided>
**Impact:** <what docs were updated>
```

### Rules

- Update STATE.md after every planning discussion, not just when asked
- Keep the decision log in reverse chronological order (newest first)
- Move resolved decisions from "Open Decisions" to "Decision Log"
- Move resolved blockers to the decision log too
- Status table must reflect current reality — don't let it go stale
- Open Decisions should be phrased as questions, not statements

## Reading State

When `/sentinel-planner` or `/sentinel-planner status` is invoked:

1. Read STATE.md
2. Read all four repo READMEs
3. Present a concise summary:
   - What phase we're in
   - What's done vs what's next
   - Any blockers or open decisions
4. Suggest the highest-priority next step

## Writing State

After a planning discussion:

1. Update the status table
2. Add new decisions to the decision log
3. Check off resolved open decisions
4. Add any new open decisions or blockers discovered
5. Update "Current Phase" if it changed
6. Update the "Last updated" date
