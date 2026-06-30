# Decision Flow

How planning decisions are made and propagated through docs.

## The Planning Loop

```
Read state → Identify next decision → Discuss with user → Get approval → Update all docs
```

Every planning session follows this loop. The planner never implements — it plans.

## Starting a Planning Session

When `/sentinel-planner` is invoked without a subcommand:

1. Read STATE.md and all repo READMEs
2. Check for open decisions — if any exist, surface the highest priority one
3. If no open decisions, look at repo status table:
   - Find the repo with the most "not-started" or "in-progress" items
   - Suggest the next logical step for that repo
4. Present the suggestion concisely: what to decide, why it matters, what it unblocks

## Running a Decision (`/sentinel-planner decide <topic>`)

### Phase 1: Context Gathering

Before discussing, silently read:
- The relevant repo's existing architecture doc (if any)
- The reference-documentation/links.md for related findings
- Subscription plans for cost/resource constraints
- The MVP architecture for migration context (if the decision involves existing code)
- Phase 1 summary for historical decisions

### Phase 2: Present the Decision

Frame every decision as:
1. **What we're deciding** — one sentence
2. **Constraints** — budget, free tier limits, existing decisions that constrain this one
3. **Options** (2-3 max) — each with trade-offs
4. **Recommendation** — what you'd pick and why

Keep it short. Don't write essays. The user knows the domain.

### Phase 3: Discussion

Let the user push back, ask questions, or redirect. Follow their lead. Don't repeat your recommendation if they're going a different direction.

### Phase 4: After Approval

Once the user approves a direction (explicit "yes", "do that", "sounds good", or just moving forward):

1. **STATE.md** — add to decision log, update repo status, check off open decision
2. **README.md** (relevant repo) — add to Key Decisions table, update Status
3. **ARCHITECTURE.md** (relevant repo) — update or create the relevant section
4. **links.md** — if external docs were referenced, add findings
5. **STATE.md open decisions** — add any new decisions that this one unblocked

**Do all updates in one pass.** Don't ask "should I update the docs?" — just do it.

## Decision Priority Order

When multiple decisions are open, prioritize by dependency:

1. **Deploy target / hosting** — everything else depends on where things run
2. **Data flow / event routing** — how components talk to each other
3. **Data model / schema** — what the data looks like
4. **API contracts** — how services expose functionality
5. **CI/CD pipeline** — how code gets deployed
6. **Testing strategy** — how we verify correctness
7. **Monitoring / observability** — how we see what's happening

## Cross-Repo Decisions

Some decisions affect multiple repos. When this happens:
- Update all affected repo READMEs
- Update all affected architecture docs
- Note the cross-repo impact in the STATE.md decision log

Example: "Deploy target is Azure App Service" affects sentinel-deployment (where the app runs), sentinel-infra (Terraform modules), and sentinel (GHA workflows that trigger deploys).

## When to Create New Docs

- **ARCHITECTURE.md**: Create when enough decisions exist to write a coherent architecture for a repo. Don't create with one decision — wait until there's substance.
- **TODO.md**: Create only when the architecture doc is marked "final" in STATE.md. Premature TODOs are waste.
- **testing/**: Create when architecture is final and you know what to test.
- **Story reports**: Create after implementation, never during planning.

## Resuming After a Break

When the user returns to planning after working on other things:
1. Read STATE.md — it has everything
2. Check if any external setup was completed (blockers resolved)
3. Surface the next open decision or suggest progress on the most advanced repo
4. Don't recap the entire history — just state where we are and what's next
