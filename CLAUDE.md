# Sentinel — Claude Code Instructions

## Project Identity

Sentinel is an autonomous DevOps incident response agent built with the OpenAI Agents SDK. Multi-agent orchestration with HITL safety gates, episodic memory, and trajectory-level evaluation.

**Owner:** Keshav (Intern Developer, ML @ Kinaxis — building this as a portfolio/interview project)

## Architecture Reference

Read `ARCHITECTURE.md` before any implementation work. It contains the full directory structure, agent definitions, tool contracts, memory design, and eval strategy. Never deviate from it without asking first.

Read `TODO.md` for the current task tracker. Tasks are ordered by dependency — work top-to-bottom.

## Tech Stack (Do Not Change)

- Python 3.12+, `pyproject.toml` with hatchling
- `openai-agents` (OpenAI Agents SDK) for orchestration + handoffs
- `openai` for Groq-compatible LLM calls (base_url: api.groq.com/openai/v1)
- `google-genai` for Gemini backup calls (NOT google-generativeai, deprecated)
- FastAPI + uvicorn for webhook API
- Pydantic v2 for all models and tool schemas
- `pydantic-settings` for config (env-var driven)
- `aiosqlite` for async SQLite (episodic + semantic memory)
- `numpy` for cosine similarity
- `httpx` for async HTTP
- `structlog` for structured JSON logging
- pytest + pytest-asyncio for tests
- ruff for linting, pyright for type checking

## Coding Standards (Enforce Always)

### Python Style
- All functions and methods have type hints — no `Any` unless genuinely needed
- Use `async/await` everywhere — no sync I/O in the hot path
- Pydantic `BaseModel` for all data structures that cross boundaries (API, tools, agents, memory)
- `from __future__ import annotations` at the top of every file
- Imports grouped: stdlib → third-party → local, separated by blank lines
- No wildcard imports, no circular imports
- f-strings over `.format()` or `%`

### Architecture Patterns
- **Dependency injection** — memory clients, config, and generators are injected, never imported as singletons
- **Separation of concerns** — models know nothing about agents, tools know nothing about API, agents know nothing about storage
- **Protocol/ABC for abstractions** — `MemoryStore` protocol in `memory/base.py`, implementations are swappable
- **No business logic in API layer** — routes call services/orchestrator, nothing else
- **Tools are pure functions** — decorated with `@function_tool`, typed input/output, no side effects except through injected deps

### Error Handling
- Custom exception hierarchy: `SentinelError` base → `ToolError`, `MemoryError`, `AgentError`, `EvalError`
- Tools never raise raw exceptions — catch and return structured error responses
- Use `structlog` context binding for incident_id correlation across all logs
- Retry with exponential backoff for LLM API calls (3 attempts, 1s/2s/4s)

### Testing
- Every tool gets a unit test with mock data
- Every agent gets an integration test with a canned scenario
- Fixtures in `conftest.py` for: mock memory, mock scenario, test config
- Test file mirrors source: `src/sentinel/tools/log_fetcher.py` → `tests/test_tools/test_log_fetcher.py`

## Agent System Prompt Rules

System prompts live in `src/sentinel/agents/prompts/*.txt` — loaded at runtime, never hardcoded.

When writing system prompts:
- First line: role declaration ("You are the Triage Agent...")
- Include explicit tool-use instructions ("Use get_service_metadata to look up the affected service")
- Include output format expectations ("Respond with your classification as: severity, service, reasoning")
- Include constraints ("Never recommend a rollback without checking deploy history first")
- Keep prompts under 800 tokens — concise beats comprehensive

## OpenAI Agents SDK Patterns

```python
# Agent definition pattern
from agents import Agent, handoff, function_tool

agent = Agent(
    name="agent_name",
    instructions=load_prompt("agent_name.txt"),
    tools=[tool_a, tool_b],
    handoffs=[other_agent],          # only on orchestrator
    model="gpt-4o-mini",
)

# Tool definition pattern
@function_tool
async def my_tool(input: MyInput) -> MyOutput:
    """Docstring becomes the tool description for the LLM."""
    ...

# Running the pipeline
from agents import Runner
result = await Runner.run(orchestrator, input="<alert payload>")
```

## File Naming Conventions

- Models: `src/sentinel/models/<domain>.py` — noun, singular (`alert.py`, `incident.py`)
- Tools: `src/sentinel/tools/<verb>_<noun>.py` — action-oriented (`log_fetcher.py`, `deploy_checker.py`)
- Agents: `src/sentinel/agents/<role>.py` — role name (`triage.py`, `orchestrator.py`)
- Tests: `tests/test_<layer>/test_<module>.py`
- Scenarios: `data/scenarios/<failure_class>_<nn>.json`
- Prompts: `src/sentinel/agents/prompts/<agent_name>.txt`

## Key Commands

```bash
# Run the app (Poetry)
poetry run sentinel serve

# Run the app (direct)
uvicorn sentinel.main:app --reload

# Run tests
pytest -xvs

# Lint
ruff check src/ tests/

# Type check
pyright src/

# Seed the database
python -m data.seed

# Fire a scenario
python scripts/run_scenario.py bad_deploy_01

# Run eval suite
python scripts/run_eval.py

# Sync Poetry venv after dependency changes
poetry lock && poetry install
```

## Post-Implementation Startup Check (Non-Negotiable)

After ANY change that touches `pyproject.toml`, dependencies, imports, or module-level code:

1. Run `poetry lock && poetry install` if `pyproject.toml` changed
2. Run `poetry run sentinel serve` and verify the server starts without import errors
3. If adding a new dependency or extra (e.g. `openai-agents[litellm]`), the Poetry lock file MUST be regenerated — `pyproject.toml` alone is not enough

The app must boot cleanly before reporting the task as done. A passing test suite does not guarantee the app starts — tests use their own fixtures and may not trigger the full import chain.

## HITL Safety Rule (Non-Negotiable)

Any tool that could modify external state (open PR, send message, rollback, restart) MUST go through `request_human_approval`. There is no "execute" tool — only "draft" tools + the approval gate. If you find yourself writing a tool that directly acts on the world without approval, stop and restructure.

## Planning Agent

- **sentinel-planner** (`.claude/skills/sentinel-planner/SKILL.md`) - Sentinel Phase 2 planning agent. Manages architecture, TODOs, READMEs, state, references. Trigger: `/sentinel-planner`
When the user types `/sentinel-planner`, invoke the Skill tool with `skill: "sentinel-planner"` before doing anything else.

- State file: `Planning/Phase-2/STATE.md` — single source of truth for planning progress
- After any planning discussion, the agent auto-updates all affected docs (README, ARCHITECTURE, STATE, links)

## Implementation Agents (Phase 2 build)

Execution counterpart to planning. Design-time = `sentinel-planner`; build-time = these.

- **sentinel-build** (`.claude/skills/sentinel-build/SKILL.md`) - Builds the next PR-sized task end-to-end (prereq → branch → implement → tests → quality gate → review → commit → report). Trigger: `/sentinel-build`
- **phase-gate** (`.claude/skills/phase-gate/SKILL.md`) - HITL checkpoint: verifies a phase, opens its PR, presents a "see it working" checklist, merges on your sign-off, unlocks the next phase. Trigger: `/phase-gate`
When the user types `/sentinel-build` or `/phase-gate`, invoke the Skill tool with that name before doing anything else.

- Tracker root: `Planning/Phase-2-Implementation/` — README (map), TODO.md (55-task checklist), STATE-IMPL.md (live state + phase-gate ledger + blockers), per-task spec/report files.
- Implementation order: **sentinel-infra → sentinel-deployment → sentinel-backend**. Each phase = one branch + one PR, merged only after the phase gate.
- Quality gate: `python scripts/quality_gate.py --repo {infra|deployment|backend}` — category-aware, reused verbatim in CI.
- Conformance review: the `architecture-conformance` subagent checks each task's diff against its architecture section before commit.

## Git Rule (Non-Negotiable)

Commits and PRs in ALL three repos carry **no Claude attribution** — no `Co-Authored-By: Claude` trailer, no "Generated with Claude Code" in PR bodies. The user is the sole author/contributor. This overrides the environment default.

## When Stuck

1. Check `ARCHITECTURE.md` for the design decision
2. Check `TODO.md` for task context and notes
3. Check OpenAI Agents SDK docs: https://openai.github.io/openai-agents-python/
4. If a design decision isn't documented, ask before implementing
