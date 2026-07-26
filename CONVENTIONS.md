# Sentinel — Conventions & Reference

Coding standards, tech stack, naming, and commands. Referenced from [CLAUDE.md](CLAUDE.md).

> Where a Phase-2 `ARCHITECTURE.md` differs from anything here (e.g. asyncpg + pgvector
> replacing aiosqlite + numpy, scenario branches replacing `data/scenarios/`), **the
> architecture wins**. This file is the default; the architecture is binding.

## Tech Stack (do not change without asking)

- Python 3.12+, `pyproject.toml` with hatchling
- `openai-agents` (OpenAI Agents SDK) for orchestration + handoffs
- `openai` for Groq-compatible LLM calls (base_url: api.groq.com/openai/v1)
- `google-genai` for Gemini backup calls (NOT google-generativeai, deprecated)
- FastAPI + uvicorn for webhook API
- Pydantic v2 for all models and tool schemas
- `pydantic-settings` for config (env-var driven)
- `httpx` for async HTTP, `structlog` for structured JSON logging
- pytest + pytest-asyncio for tests; ruff for linting, pyright for type checking
- Phase-2 data layer: `asyncpg` + `pgvector` (Postgres); `alembic` migrations; `langfuse` tracing

## Coding Standards (enforce always)

### Python style
- Type hints on every function/method — no `Any` unless genuinely needed
- `async/await` everywhere — no sync I/O in the hot path
- Pydantic `BaseModel` for every structure that crosses a boundary (API, tools, agents, memory)
- `from __future__ import annotations` at the top of every file
- Imports grouped stdlib → third-party → local, blank-line separated; no wildcard/circular imports
- f-strings over `.format()` or `%`

### Architecture patterns
- **Dependency injection** — memory clients, config, generators are injected, never singletons
- **Separation of concerns** — models know nothing of agents; tools nothing of API; agents nothing of storage
- **Protocol/ABC for abstractions** — `MemoryStore` protocol in `memory/base.py`, swappable impls
- **No business logic in the API layer** — routes call services/orchestrator, nothing else
- **Tools are pure functions** — `@function_tool`, typed I/O, side effects only through injected deps

### Error handling
- Custom hierarchy: `SentinelError` → `ToolError`, `MemoryError`, `AgentError`, `EvalError`
- Tools never raise raw exceptions — catch and return structured error responses
- `structlog` context binding for `incident_id` correlation across all logs
- Retry with exponential backoff for LLM API calls (3 attempts, 1s/2s/4s)

### Testing
- Every tool gets a unit test with mock data; every agent an integration test with a canned scenario
- Fixtures in `conftest.py`: mock memory, mock scenario, test config
- Test file mirrors source: `src/sentinel/tools/log_fetcher.py` → `tests/test_tools/test_log_fetcher.py`

## Agent System Prompt Rules

System prompts live in `src/sentinel/agents/prompts/*.txt` — loaded at runtime, never hardcoded.
- First line: role declaration ("You are the Triage Agent…")
- Explicit tool-use instructions ("Use `get_service_metadata` to look up the affected service")
- Output-format expectations ("Respond with: severity, service, reasoning")
- Constraints ("Never recommend a rollback without checking deploy history first")
- Keep prompts under 800 tokens — concise beats comprehensive

## OpenAI Agents SDK Patterns

```python
from agents import Agent, handoff, function_tool

agent = Agent(
    name="agent_name",
    instructions=load_prompt("agent_name.txt"),
    tools=[tool_a, tool_b],
    handoffs=[other_agent],          # only on orchestrator
    model="gpt-4o-mini",
)

@function_tool
async def my_tool(input: MyInput) -> MyOutput:
    """Docstring becomes the tool description for the LLM."""
    ...

from agents import Runner
result = await Runner.run(orchestrator, input="<alert payload>")
```

## File Naming Conventions

- Models: `src/sentinel/models/<domain>.py` — noun, singular (`alert.py`, `incident.py`)
- Tools: `src/sentinel/tools/<verb>_<noun>.py` — action-oriented (`log_fetcher.py`, `deploy_checker.py`)
- Agents: `src/sentinel/agents/<role>.py` — role name (`triage.py`, `orchestrator.py`)
- Tests: `tests/test_<layer>/test_<module>.py`
- Prompts: `src/sentinel/agents/prompts/<agent_name>.txt`

## Key Commands

```bash
poetry run sentinel serve            # run the app (Poetry)
uvicorn sentinel.main:app --reload   # run the app (direct)
pytest -xvs                          # run tests
ruff check src/ tests/               # lint
pyright src/                         # type check
python scripts/run_eval.py           # run eval suite
poetry lock && poetry install        # sync venv after dependency changes
```

### Post-implementation startup check (non-negotiable)
After ANY change to `pyproject.toml`, dependencies, imports, or module-level code:
1. `poetry lock && poetry install` if `pyproject.toml` changed
2. `poetry run sentinel serve` — verify the server starts without import errors
3. A new dependency/extra (e.g. `openai-agents[litellm]`) **requires** regenerating the lock file —
   `pyproject.toml` alone is not enough

The app must boot cleanly before a task is "done". A passing test suite does not guarantee it —
tests use their own fixtures and may not trigger the full import chain.
