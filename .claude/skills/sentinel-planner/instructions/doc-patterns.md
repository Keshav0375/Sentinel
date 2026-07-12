# Document Patterns

Each doc type in the planning system serves a specific purpose. Follow these patterns exactly.

## 1. README.md (per repo folder)

**Purpose:** Short index and current state of planning for this repo. Works as a landing page.

**Structure:**
```markdown
# <repo-name>

<2-3 sentence description of what this repo does>

## What This Repo Will Contain
- <bullet list of key components>

## Key Decisions
| Decision | Choice | Rationale |
|----------|--------|-----------|
| Deploy target | Azure App Service F1 | Free tier, no payment method needed |

## Key Docs
| Doc | Purpose |
|-----|---------|
| ARCHITECTURE.md | Full technical architecture |

## Status
<current state — what's done, what's next, what's blocked>
```

**Rules:**
- Keep under 80 lines — it's an index, not the architecture doc
- Update "Status" section after every relevant discussion
- "Key Decisions" table grows as decisions are made — never remove entries
- Link to architecture doc for deep details, don't duplicate content

## 2. ARCHITECTURE.md (per repo folder)

**Purpose:** Full technical architecture — the implementation guide. A developer should be able to build the system from this doc alone.

**Structure:**
```markdown
# <repo-name> — Architecture Document

> Purpose: <one-sentence purpose>

## 1. System Overview
<diagram + explanation of the full system>

## 2-N. <Domain sections>
<Each major component gets its own numbered section>
<Include: data models, API contracts, flow diagrams, config schemas>

## N+1. Prerequisites & Setup Checklist
- [ ] <setup step>

## N+2. Cost Breakdown
| Resource | Cost | Covered By |
|----------|------|------------|
```

**Rules:**
- Numbered sections, not nested headers
- Include ASCII diagrams for system flows
- Every external service gets a table: what, tier, cost, free limit
- Config schemas use actual field names and types
- Prerequisites are checkboxes — they're actionable
- Don't write architecture until decisions are finalized in discussions
- Update when decisions change — architecture reflects current truth

### 2a. `Planning/Phase-2/ARCHITECTURE.md` — the master **Architecture Index** (special)

The master doc is **not** a fourth full architecture — it is a lean, diagram-first **index**
that agents and skills land on first for the whole picture, then leave via pointers.

**Rules:**
- Keep it **short and high-level**: system diagrams (three-repo, end-to-end), a one-paragraph
  summary per cross-cutting concern, and a **concern → authoritative file+§ map**.
- **Do not duplicate deep detail** that lives in a per-repo `*/ARCHITECTURE.md`. If a concern
  grows detail, put the detail in the per-repo file and leave only a summary + `→ deep dive`
  pointer here. When a decision changes contracts, edit the **per-repo** file (authoritative);
  update the index only if the diagram/summary/map changed.
- Per-task **Arch refs** (in the implementation tracker) point into the **per-repo** files, not
  the index. The `architecture-conformance` agent verifies against those per-repo sections.
- Keep the §4 map and the per-repo file section numbers in sync when sections are added/renamed.

## 3. TODO.md (per repo folder — created when architecture is final)

**Purpose:** Implementation task tracker. Each task maps to code that needs to be written.

**Structure:**
```markdown
# <repo-name> — Implementation Tasks

> Each task is one PR-sized unit of work. Check off as completed.

## Phase N.M — <section name> (X-Y%)

### [ ] N.M.K — <task title> (Z%)
<What to implement. Specific files, functions, contracts.>
<Acceptance criteria: what "done" looks like.>
```

**Rules:**
- Tasks are ordered by dependency — work top to bottom
- Each task has a percentage weight (total = 100%)
- Tasks reference specific files and function signatures from ARCHITECTURE.md
- Only create TODOs when the architecture doc is finalized
- Never create TODOs during active planning discussions — premature

## 4. reference-documentation/links.md

**Purpose:** External docs, API references, and findings from reading those docs.

**Structure:**
```markdown
## <Topic>

### Docs
| Doc | URL | What It Covers |
|-----|-----|----------------|

### Key Findings
<What we learned that affects our architecture>

### Setup Steps (if applicable)
<Step-by-step from the docs, adapted to our context>
```

**Rules:**
- Group by topic (Datadog, Azure, GHA, etc.), not chronologically
- Include findings — why we read the doc, what we learned
- Link setup steps to the architecture doc that uses them
- Add new entries whenever external docs are referenced in discussions

## 5. Story Reports (Planning/MVP-Story-Reports/ pattern)

**Purpose:** Post-implementation record of what was built and why for each TODO task.

**Structure:**
```markdown
# Story N.M — <title>

## What Changed
<Files created/modified, with brief description>

## Technical Decisions
<Any choices made during implementation, with rationale>

## Testing
<How it was tested, what passed>
```

**Rules:**
- One story report per completed TODO task
- Created AFTER implementation, not during planning
- Phase 2 story reports go in `Planning/Phase-2-Story-Reports/` (create when needed)
- Keep factual — what happened, not what was planned

## 6. testing/ (per repo folder — created when architecture is final)

**Purpose:** Testing architecture for CI workflows, unit tests, and integration tests.

**Structure:**
```markdown
# <repo-name> — Testing Architecture

## CI Workflow
<What runs on PR, what runs on merge>

## Unit Tests
<What gets unit tested, fixtures, mocking strategy>

## Integration Tests
<End-to-end scenarios, external service mocking>

## Test Data
<Fixtures, scenarios, seed data>
```

**Rules:**
- Testing doc is separate from architecture — testing decisions deserve their own space
- Only create when the architecture is stable enough to know what to test
- CI workflow section must match the actual GHA YAML

## 7. Subscription Plans (Planning/Subscription-plans/)

**Purpose:** Read-only reference for what cloud resources and credits are available.

**Rules:**
- These are reference docs — the planner reads them, never writes to them
- When making cost/resource decisions, cite the specific line from these docs
- If resource availability changes, update the reference doc first, then update architecture docs
