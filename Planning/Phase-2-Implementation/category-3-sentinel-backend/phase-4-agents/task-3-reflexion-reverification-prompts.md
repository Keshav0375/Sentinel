# task-3 — `reflexion.txt` + `reverification.txt` prompts   ·   [category-3-sentinel-backend / phase-4-agents]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `impl/backend-phase-4-agents` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §4.3, §4.4, §13.3; CLAUDE.md system-prompt rules |
| **Depends on** | — (can precede the orchestrator; orchestrator loads them) |
| **Referenced by** | [[task-2-orchestrator-loops]] |

## Spec
Add the two new agentic-loop prompts as files (loaded at runtime; LangFuse-first per §6.3).

**Files created:**
- `src/sentinel/agents/prompts/reflexion.txt` — the critique prompt (§4.3): confidence 0.0–1.0, supporting/missing evidence, next tool call if < 0.7.
- `src/sentinel/agents/prompts/reverification.txt` — the PASS/FAIL/ESCALATE check (§4.4): fix addresses root cause? target deploy correct? side effects?
- Follow CLAUDE.md prompt rules: role first line, output-format expectations, constraints, < 800 tokens.

## Prerequisites
- [ ] none (text files).

## Acceptance Criteria
- [ ] Both prompts exist, load via the prompt loader with file fallback, and specify the exact output contract the orchestrator parses (confidence float; PASS/FAIL/ESCALATE token).
- [ ] Under 800 tokens each; no hardcoding in code.

## Tests
- **Unit:** prompt loader returns each file; a parse test confirms the documented output tokens are described.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `load_prompt("reflexion")` / `("reverification")` return the file contents.
2. Orchestrator tests (4.2) consume them successfully.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none._
