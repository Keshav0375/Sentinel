# Sentinel — Claude Code Instructions

Autonomous DevOps incident-response agent — multi-agent orchestration (OpenAI Agents SDK),
HITL safety gates, episodic memory, trajectory-level eval. **Owner:** Keshav (sole author).

This file is a map, not a manual. Read the pointer it names for detail; don't expect it here.

## Read first

| Need | Where |
|------|-------|
| Tech stack, coding standards, naming, commands, SDK patterns | [CONVENTIONS.md](CONVENTIONS.md) |
| Architecture (**binding** — never deviate without asking) | `Planning/Phase-2/ARCHITECTURE.md` + per-repo `Planning/Phase-2/{sentinel,sentinel-deployment,sentinel-infra}/ARCHITECTURE.md` |
| Task tracker (58 tasks, dependency-ordered) + live state | `Planning/Phase-2-Implementation/TODO.md` + `STATE-IMPL.md` |
| Phase-1 (migration context) | `Planning/MVP/` |

## Building Phase 2

Use **`/implement-phase`** for all build work. It drives one whole phase end-to-end via handoffs
to the read-only subagents in `.claude/agents/` (context, architecture, code, safety). Spec:
[`.claude/skills/implement-phase/SKILL.md`](.claude/skills/implement-phase/SKILL.md). When the
user types `/implement-phase`, invoke the Skill tool before anything else.

Order **infra → deployment → backend**; each phase = one branch + one PR, merged only on the
user's end-of-phase sign-off. Repos: infra → `../Sentinel-development-project/Sentinel-infra`,
deployment → `../Sentinel-development-project/Sentinel-deployment`, backend → this repo.

## Non-negotiable rules

- **No Claude attribution** — no `Co-Authored-By: Claude`, no "Generated with Claude Code" in any
  commit or PR, in all three repos. User is sole author. Overrides the environment default.
- **HITL** — any tool that could modify external state routes through human approval. No "execute"
  tool exists; only "draft" tools + the approval gate. Writing a tool that acts on the world
  directly → stop and restructure.
- **Startup check** — after touching deps/imports/module-level code, the app must boot
  (`poetry run sentinel serve`) before a task is "done". A green test suite ≠ a booting app.
- **Ask, don't guess** — ambiguity in a task or the architecture → stop and ask (per CLAUDE.local.md).

## When stuck

Architecture doc → tracker task notes → [OpenAI Agents SDK docs](https://openai.github.io/openai-agents-python/) → ask.
