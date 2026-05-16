# Sentinel — Multi-Provider Model Layer: Execution Plan for Claude Code

> Read this fully before touching any file.
> Work top to bottom. Each step must pass `ruff check` + `pyright` before moving on.

---

## Context

Sentinel currently hard-codes Groq everywhere. We are replacing that with a
`provider/model` string system so any agent can be pointed at any supported
provider via one env-var change. We also fix three Groq handoff bugs in the
same pass.

**Providers to support (5):**
- `openai/` → Direct OpenAI
- `azure/` → Azure OpenAI
- `anthropic/` → Direct Anthropic
- `groq/` → Groq (Llama, Mixtral, Gemma, any model Groq serves)

**Gemini is explicitly excluded** — will be added later with Google ADK.

**Bridge:** LiteLLM (`openai-agents[litellm]`) handles all routing.
Model strings use LiteLLM's native `provider/model` format.

---

## Step 1 — Fix the three Groq handoff bugs

**Files:** `src/sentinel/main.py`, `src/sentinel/tools/*.py`

1. In `main.py` lifespan, add these two calls before any agent is built:
   ```python
   from agents import set_default_openai_api, set_tracing_disabled
   set_default_openai_api("chat_completions")
   if not settings.openai_api_key:          # openai_api_key added in Step 2
       set_tracing_disabled(True)
   ```

2. In every `@function_tool` decorator across `src/sentinel/tools/` add
   `strict_json_schema=False`. Files to sweep:
   - `comms_tools.py`, `deploy_checker.py`, `hitl.py`,
     `incident_search.py`, `log_fetcher.py`, `remediation_tools.py`,
     `service_lookup.py`

   Example change:
   ```python
   # before
   @function_tool
   # after
   @function_tool(strict_json_schema=False)
   ```
   For tools already using `@function_tool(strict_mode=False)` like
   `log_fetcher.py`, add `strict_json_schema=False` to the same call.

3. In `config.py`, increase `max_turns` passed to `Runner.run`:
   change `max_turns=settings.sentinel_max_tool_calls` to
   `max_turns=settings.sentinel_max_tool_calls + 5` everywhere it appears
   (`main.py`, `run_scenario.py`, `run_eval.py`).

4. In `orchestrator.txt` prompt, append one line to the Handoff Rules section:
   `- When calling transfer_to_<agent>, pass no arguments.`

**Verify:** Run `pytest tests/test_tools/ -x` — all pass.

---

## Step 2 — Update `config.py` and `.env.example`

**Files:** `src/sentinel/config.py`, `.env.example`

1. Add these fields to the `Settings` class, replacing the existing
   `sentinel_triage_model`, `sentinel_analysis_model`, `sentinel_judge_model`
   fields (keep the field names, change types/defaults):

   ```python
   # Model strings — provider/model format (LiteLLM)
   sentinel_triage_model: str = "groq/llama-3.1-8b-instant"
   sentinel_analysis_model: str = "groq/llama-3.3-70b-versatile"
   sentinel_judge_model: str = "groq/llama-3.1-8b-instant"

   # API keys — one per provider, all optional at field level
   openai_api_key: str = ""
   anthropic_api_key: str = ""
   azure_api_key: str = ""
   azure_api_base: str = ""
   azure_api_version: str = "2024-02-01"
   # groq_api_key already exists — keep it
   ```

2. Add a `@model_validator(mode="after")` that reads the provider prefix from
   each of the three model strings and asserts the matching API key is non-empty.
   Fail with a clear message like:
   `"SENTINEL_ANALYSIS_MODEL=anthropic/... requires ANTHROPIC_API_KEY to be set"`

3. Rewrite `.env.example` to reflect all new vars. Group by section:
   `── Models`, `── Groq`, `── OpenAI`, `── Anthropic`, `── Azure`.
   Keep all existing vars. Add the new ones with inline comments explaining
   the `provider/model` format and pointing to `available_models.md`.

**Verify:** Existing config tests pass. Add 3 new tests covering the validator
(missing key for active provider → raises, correct key → passes).

---

## Step 3 — Create `src/sentinel/providers/` package

**New files:**
```
src/sentinel/providers/__init__.py
src/sentinel/providers/resolver.py
src/sentinel/providers/capabilities.py
```

1. `resolver.py` — one public function:
   ```python
   def resolve_model(model_string: str, settings: Settings) -> Model:
       """
       "groq/llama-3.1-8b-instant" → LitellmModel(model=..., api_key=...)
       "azure/gpt-4o-deployment"   → LitellmModel + sets AZURE_API_BASE etc in os.environ
       """
   ```
   - Parse provider prefix from `model_string.split("/")[0]`
   - Map to the right `settings.*_api_key` field
   - For `azure`: also set `os.environ["AZURE_API_BASE"]` and
     `os.environ["AZURE_API_VERSION"]` from settings before returning
     (LiteLLM reads these from env)
   - Return `LitellmModel(model=model_string, api_key=api_key)`
   - Raise `ValueError` for unknown prefix

2. `capabilities.py` — one public function:
   ```python
   def get_capabilities(model_string: str) -> ProviderCapabilities:
   ```
   Capability table:
   - `openai`: structured_outputs=True, strict_schemas=True
   - `azure`: structured_outputs=True, strict_schemas=True
   - `anthropic`: structured_outputs=False, strict_schemas=False
   - `groq`: structured_outputs=True, strict_schemas=False
   - unknown prefix: both False (safe default)

3. `__init__.py` — re-export `resolve_model`, `get_capabilities`,
   `apply_sdk_defaults` (move the two lifespan calls from Step 1 here as a
   named function so `main.py` calls `apply_sdk_defaults(settings)` cleanly).

4. Add `openai-agents[litellm]` to `pyproject.toml` dependencies
   (replace `openai-agents>=0.0.14`).

**Verify:** Add `tests/test_providers/` with:
- `test_resolver.py` — resolve each of 4 providers, assert returns `LitellmModel`
- `test_capabilities.py` — assert correct flags per provider
- `test_unknown_prefix.py` — assert `ValueError` on bad string

---

## Step 4 — Migrate all 6 agent builders

**Files:** `src/sentinel/agents/{triage,log_analyst,deploy_correlator,remediation,comms,orchestrator}.py`,
`src/sentinel/main.py`, `scripts/run_scenario.py`, `scripts/run_eval.py`

1. Change every `build_*_agent` signature:
   - Remove `groq_client: AsyncOpenAI`
   - Remove `model_name: str`
   - Add `model_string: str` and `settings: Settings`
   - Inside, call `llm = resolve_model(model_string, settings)` and
     `caps = get_capabilities(model_string)`

2. For `output_type=` — wrap in capability check:
   ```python
   kwargs: dict[str, Any] = {}
   if caps.supports_structured_outputs:
       kwargs["output_type"] = SomeResultType
   return Agent(..., model=llm, **kwargs)
   ```

3. Add `output_coercion.py` to `src/sentinel/providers/`:
   ```python
   def coerce_output(text: str, schema: type[BaseModel]) -> BaseModel | None:
       """Parse plain-text agent output as Pydantic model. Used when
       structured outputs are not supported (e.g. anthropic provider)."""
   ```
   Strip markdown fences, attempt `model_validate_json`, return None on failure.

4. In `main.py` lifespan:
   - Remove all `AsyncOpenAI(...)` and `set_default_openai_client(...)` calls
   - Remove `OpenAIChatCompletionsModel` imports from agent files
   - Replace with `apply_sdk_defaults(settings)` (from providers package)
   - Pass `model_string=settings.sentinel_triage_model, settings=settings`
     to each builder

5. Mirror same changes in `run_scenario.py` and `run_eval.py`.

6. Update `tests/test_agents/conftest.py` — replace `groq_client=AsyncOpenAI(...)`
   fixture with a `FakeSettings` fixture and a stub that returns a `MagicMock`
   `Model` from `resolve_model`. No live API calls in unit tests.

**Verify:** `pytest tests/test_agents/ -x` — all pass (structural tests only,
no live calls).

---

## Step 5 — Create reference files + update docs

**New files:** `available_models.md`, `todo_model_provider.md`
**Update:** `architecture.md`, `README.md`

1. Create `available_models.md` at project root. Structure:
   - One section per provider
   - For each provider: table of recommended models with role
     (triage / analysis / judge), context window, and the exact
     env-var string to use
   - Include a "quick swap" examples block at the top

2. Create `todo_model_provider.md` at project root. 5 tasks × 20%:
   - Task 1 (20%): Handoff bug fixes (Step 1 above) `[ ]`
   - Task 2 (20%): Config + .env update (Step 2 above) `[ ]`
   - Task 3 (20%): providers/ package (Step 3 above) `[ ]`
   - Task 4 (20%): Agent builder migration (Step 4 above) `[ ]`
   - Task 5 (20%): Reference files + docs + final clean pass (Step 5 above) `[ ]`

3. Update `architecture.md`:
   - In §3 Tech Stack: replace the Groq-specific LLM line with:
     `LLM: LiteLLM (openai-agents[litellm]) — provider/model string selects
     provider at runtime. Defaults to Groq. See available_models.md.`
   - Add a new §3.1 `Provider Layer`:
     - Describe the `provider/model` format
     - List the 5 supported providers + their prefixes
     - Note Gemini excluded pending Google ADK plan
     - Reference `src/sentinel/providers/`

4. Update `README.md` Tech Stack table — replace the Groq-only LLM row with
   the multi-provider row. Add a short "Switching providers" section to Quick
   Start showing a 3-line `.env` change example.

5. Final clean pass:
   - `ruff check src/ tests/ --fix`
   - `pyright src/`
   - `pytest -x --tb=short`
   - Assert no `AsyncOpenAI` or `OpenAIChatCompletionsModel` import remains
     in `src/sentinel/agents/`

---

## Acceptance Criteria

- [ ] `pytest -xvs` green — zero regressions from current main
- [ ] No `AsyncOpenAI` or `OpenAIChatCompletionsModel` import in `src/sentinel/agents/`
- [ ] `ruff check src/ tests/` clean
- [ ] `pyright src/` clean
- [ ] `SENTINEL_ANALYSIS_MODEL=anthropic/claude-sonnet-4-6` runs
      `python scripts/run_scenario.py bad_deploy_01` end-to-end
- [ ] Same smoke test passes for `openai/gpt-4o` and `groq/llama-3.3-70b-versatile`
- [ ] `available_models.md`, `todo_model_provider.md` exist at project root
- [ ] `architecture.md` §3.1 added