# task-4 — `pr_content_generator` agent + prompt   ·   [category-3-sentinel-backend / phase-4-agents]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `impl/backend-phase-4-agents` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §3.4 (contract + prompt rules), §13.3 |
| **Depends on** | [[task-1-provider-routing]] |
| **Referenced by** | [[task-3-generate-pr-content-endpoint]] |

## Spec
The agent that writes the **rollback** PR title + description during an incident (the only
consumer is `ci_incident_response.yml`). No tools — pure constrained generation, Haiku.

**Files created:**
- `src/sentinel/agents/pr_content_generator.py` — Agent def, model `anthropic/claude-haiku-4-5` (fb `openai/gpt-4o-mini`), no tools, traced as `pr-content-generation` span.
- `src/sentinel/agents/prompts/pr_content_generator.txt` — per §3.4 key instructions: title `revert:` prefix < 72 chars with PR# + incident id; description sections Incident/Root Cause/Evidence/Rollback; state confidence plainly; 100–200 words.

## Prerequisites
- [ ] task 4.1 providers. [ ] fake LLM for tests.

## Acceptance Criteria
- [ ] Given the §3.4 input (root_cause, evidence, confidence, target_deploy) produces `{title, description, model_used, tokens_used}`.
- [ ] Title format + description sections match §3.4; no tools attached.

## Tests
- **Unit (`tests/test_agents/test_pr_content_generator.py`, fake LLM):** output has a `revert:`-prefixed title with PR# + incident id, all four description sections, confidence stated.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate — end of Category 3 Phase 4)
1. `pytest tests/test_agents/test_pr_content_generator.py -q` green (fake).
2. (with B4) a real Haiku call returns a well-formed revert PR body.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live call ⛔ B4. Agent + prompt + fake tests writable now._
