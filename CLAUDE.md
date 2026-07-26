# Sentinel — backend

The multi-agent DevOps incident-response backend. **Code + CI only.**

**This repo is not driven from inside itself.** Architecture, the implementation tracker, the
build agents and phase reports live in the **`sentinel-brain`** repo — a sibling checkout at
`../sentinel-brain` (GitHub: `Keshav0375/sentinel-brain`). Open *that* repo to plan, build, or
review Phase-2 work; `/implement-phase` runs from there and edits this repo from outside.

| Need | Where |
|------|-------|
| Coding standards, tech stack, naming, commands | [CONVENTIONS.md](CONVENTIONS.md) (in this repo) |
| Architecture (**binding**) | `../sentinel-brain/architecture/backend.md` |
| What to build next, task specs, status | `../sentinel-brain/implementation/` |
| Quality gate | `scripts/quality_gate.py` (lives here — CI calls it directly) |

## Rules that apply to changes in this repo

- **No planning docs, agents, skills, trackers or reports here.** They belong in
  `sentinel-brain`. Keep this repo shippable code.
- **No Claude attribution** — no `Co-Authored-By: Claude`, no "Generated with Claude Code" in
  any commit or PR. The user is sole author. Overrides the environment default.
- **HITL** — no tool may modify external state. The backend reasons and drafts; GitHub Actions
  executes; the revert PR is the human gate. A tool that acts on the world → stop and restructure.
- **Branch model** — branch from `release-phase-2` as `dev/<slug>`, PR back into
  `release-phase-2`. Never branch from or PR to `main`; `.github/workflows/guard-main-source.yml`
  enforces it.
- **Startup check** — after touching deps/imports/module-level code, `poetry run sentinel serve`
  must boot before a task is done. A green test suite ≠ a booting app.
- **Green means green** — `python scripts/quality_gate.py --repo backend` reporting
  `INCONCLUSIVE`, or `PASS` with skipped checks, is not a pass. Say what did not run.

> `data/`, `src/sentinel/generator/`, `db.py` and the Phase-1 scripts are **legacy Phase-1
> code**, still here only so Phase 1 keeps working until Phase 2 replaces it. Backend task 9.1
> deletes them. Don't build on them.
