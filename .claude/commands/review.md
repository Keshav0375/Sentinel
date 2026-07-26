---
description: Ad-hoc review of uncommitted or recent changes against Phase-2 standards and safety invariants
allowed-tools: Read, Bash, Grep, Glob
model: sonnet
---

Review the current changes in this repo against the **Phase 2** architecture and safety model.

> This is the *ad-hoc* review you run mid-task. The authoritative end-of-phase review is
> `/implement-phase` Step 5, which runs `architecture-warden`, `code-reviewer` and
> `safety-reviewer` over the whole phase diff. Use this for a quick read between commits.

1. Run `git diff --name-only`. If there are no uncommitted changes, use `git diff HEAD~1 --name-only`.

2. **Coding standards** (CONVENTIONS.md) for each changed file:
   - `from __future__ import annotations`; type hints everywhere; no gratuitous `Any`
   - `async/await` in the hot path — no sync I/O blocking the loop
   - Pydantic v2 `BaseModel` at every boundary (API, tools, agents, memory)
   - Tools return structured errors — they never raise raw exceptions; LLM calls retry with backoff
   - Dependency injection, no singletons; `structlog` with `incident_id` bound (never `print`)
   - Prompts loaded from `agents/prompts/*.txt`, never hardcoded
   - Style-match: the new code reads like the code around it

3. **Phase-2 safety invariants** — flag any violation as 🚨 CRITICAL:
   - **No-execute boundary.** The backend reasons and drafts; **GitHub Actions executes.**
     There is no "execute" tool, no `request_human_approval` gate, and no `/approvals` endpoint
     in Phase 2 — those are Phase-1 artifacts (removed in backend task 3.7 / sentinel §13).
     A tool that calls the GitHub API to open a PR, posts to Teams, deploys, restarts or rolls
     back is a blocker. `prepare_rollback_spec` and `format_escalation` **return data**; they
     act on nothing.
   - **HITL gate = the revert PR.** The only destructive action is a revert PR on
     `sentinel-deployment`, opened by GHA, merged or closed by a human. Fire-and-forget: no
     waiting states, no PR-outcome tracking (sentinel §3.3, §7).
   - **Tool-call cap.** The orchestrator enforces a hard budget of **20** tool calls per incident
     (sentinel §4.6) and reflexion loops cap at **2** (§4.3). Verify the caps are actually
     enforced, not just declared — an unbounded loop is a blocker.
   - **No self-grading.** The eval judge must be a different model/family than the agents it scores.
   - **Memory provenance.** Episodic writes record which agent and which incident produced them.
   - **Secret hygiene.** No secrets logged or committed. DB auth is a short-lived **Entra token**
     (there is no `db-password`); API auth is an **Entra bearer** validated against JWKS (there is
     no `X-Sentinel-Token` / `SENTINEL_API_TOKEN`). Finding either of those names in new code is
     a blocker.

4. **Tests** — every new tool/agent has a test file mirroring its source path, and the test
   actually exercises behavior rather than just importing. Confirm the file lands in a package
   the gate runs (`scripts/quality_gate.py` MATRIX) — a test in an unlisted directory never runs.

5. **Imports** — no circular or wildcard imports; grouped stdlib → third-party → local.

Summarize as:
- ✅ What looks good
- ⚠️ Suggestions
- 🚨 Critical issues (safety-invariant violations first, with `file:line` and the concrete failure each enables)
