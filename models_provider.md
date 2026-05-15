# Sentinel — Multi-Provider Model Layer

> Implementation plan for swapping the LLM provider (OpenAI / Groq / Gemini / Anthropic) via a single env var, while keeping the OpenAI Agents SDK orchestration + handoff machinery intact. Companion to `architecture.md` §3 (Tech Stack) and §5 (Agents).

**Status:** plan only — no code yet. Approval gate before implementation.

---

## 1. Goal

Replace the hard-coded "Groq + `AsyncOpenAI` + `OpenAIChatCompletionsModel`" wiring in every `build_*_agent` function with a `ModelProvider` abstraction. A single env var `SENTINEL_LLM_PROVIDER ∈ {openai, groq, gemini, anthropic}` selects the active provider at startup. All five specialists + orchestrator stay framework-agnostic at construction time — they ask the provider for a `Model` object, they do not know which vendor is behind it.

Secondary goal: fix the agent-handoff errors we are currently hitting on Groq. The root causes are independent of the provider abstraction but the same plan addresses both, because the provider classes are the right place to apply the per-provider workarounds (chat-completions pin, `strict=False`, tracing disable).

Non-goals:
- Not switching orchestration frameworks (no Google ADK, no `claude-agent-sdk`).
- Not removing LiteLLM — we use it for Anthropic only (Anthropic does not ship an OpenAI-compatible endpoint).
- Not changing tool, memory, or API layers. Tools stay pure functions; memory stays SQLite.

---

## 2. Why this works — research summary

Key facts pulled from the official OpenAI Agents SDK docs (`docs/models/index.md`, `docs/handoffs.md`) and source (`src/agents/models/interface.py`, `extensions/models/litellm_model.py`):

1. **Agents SDK is provider-agnostic at the `Model` layer.** `Agent(model=...)` accepts a string, a `Model` instance, or a name resolved by a `ModelProvider`. Per-agent assignment is a first-class pattern in the docs.
2. **Handoffs are emitted as regular function-call tools** (`transfer_to_<agent_name>`). They do not depend on the OpenAI Responses API or any OpenAI-specific feature. As long as the provider supports OpenAI-style tool calling, `handoffs=[...]` works. This is the critical fact: our current handoff errors are not framework-level — they are configuration bugs (see §4).
3. **Three legal `Model` shapes for our four providers:**
   - **OpenAI**, **Groq**, **Gemini** → `OpenAIChatCompletionsModel(model=..., openai_client=AsyncOpenAI(base_url=...))`. Gemini ships an OpenAI-compatible endpoint at `https://generativelanguage.googleapis.com/v1beta/openai/`, so no LiteLLM is needed.
   - **Anthropic** → `LitellmModel("anthropic/claude-sonnet-4-6", api_key=...)`. Requires the `openai-agents[litellm]` extra. Anthropic does not publish an OpenAI-compatible HTTP endpoint, so LiteLLM is the cleanest bridge. The native `anthropic` SDK and `claude-agent-sdk` are both poor fits — the former has no handoff primitive and would force us to re-implement orchestration, the latter wraps the Claude Code Node CLI binary and is designed for filesystem agents, not webhook responders.

Per-provider quirks the SDK or LiteLLM already handle for us: Anthropic/Gemini tool-result message-ordering, Gemini thought-signature plumbing.

Per-provider quirks **we** need to handle:
- Structured output (`output_type=PydanticModel`) is reliable on OpenAI/Groq/Gemini but emulated on Anthropic-via-LiteLLM (schema injected into the prompt, occasional malformed JSON). Solution: capability flag — see §6.
- Tracing uploads to OpenAI by default; raises 401 when `OPENAI_API_KEY` is absent. Solution: `set_tracing_disabled(True)` unless the env var is set.
- Groq's chat-completions endpoint rejects some `strict: true` JSON schemas the SDK emits. Solution: pass `strict_json_schema=False` on `@function_tool` decorators that fail, and `set_default_openai_api("chat_completions")` so the SDK never tries the Responses API on Groq.

---

## 3. Architectural decision (confirmed)

**Option A — `providers/` package + Agents SDK.** Per the four user-confirmed choices:

| Decision | Choice |
|---|---|
| Layer architecture | Thin per-provider factories behind a `ModelProvider` protocol |
| Structured outputs on weak providers | Capability flag → auto-fallback (Pydantic class kept on capable providers; plain-text + local validation on Anthropic) |
| Env-var shape | Single `SENTINEL_LLM_PROVIDER` global switch (model IDs stay per-agent) |
| Handoff bugs | Diagnose + list fixes in this same plan |

Rejected alternatives and why:
- **Custom `Model` ABC adapters for every provider** — re-implements tool-call message-ordering quirks LiteLLM already handles. Saves the LiteLLM dependency but costs ~150 LOC/provider in maintenance. Not justified.
- **Framework-agnostic abstraction** — would require building our own orchestrator and re-implementing `handoffs=[...]`. Massive scope creep.
- **`set_default_openai_client` + global model name only** — loses per-agent model selection (we want llama-8b for triage, llama-70b for analysis, etc.).

---

## 4. Handoff-bug diagnosis

Three independent issues, all configuration-level. Each is fixed once in the provider classes; no per-agent code changes needed.

### 4.1 SDK defaults to the Responses API
The Agents SDK by default calls OpenAI's Responses API. Groq, Gemini-OpenAI-compat, and LiteLLM all speak chat-completions, not Responses. Symptom: 400/404 on the very first turn when handoffs are present.

**Fix:** Call `set_default_openai_api("chat_completions")` once at startup, inside the provider's `install_sdk_defaults()` hook. Already implicit when we build `OpenAIChatCompletionsModel` per-agent, but the SDK still uses the global default for some internal calls (eg. agent-as-tool wrappers), so we set it explicitly.

### 4.2 Tracing exporter 401s when `OPENAI_API_KEY` is absent
The SDK auto-uploads traces to OpenAI's servers. With Groq-only setup, this 401s repeatedly and can intermittently fail handoffs that depend on trace-span correlation.

**Fix:** `set_tracing_disabled(True)` unless `OPENAI_API_KEY` is present in `Settings`. Done once in `install_sdk_defaults()`.

### 4.3 Strict JSON schema rejected by Groq
The SDK emits `strict: true` on `@function_tool` schemas (and on the auto-generated `transfer_to_*` handoff tool). Groq's parser rejects schemas with `anyOf` at root or unsupported keywords. Symptom: `BadRequestError: tool schema invalid` on the handoff turn.

**Fix:** Add `strict_json_schema=False` to every `@function_tool(...)` decorator in `src/sentinel/tools/`. For handoffs, pass `strict_schemas=False` via `handoff(other_agent, strict_schemas=False)` or rely on the simplification when no `input_type` is set. (Confirmed by reading the SDK's `_run_impl.py` — handoff tool schemas inherit the agent's default `strict_schemas`.) Apply automatically only for providers whose `supports_strict_schemas() == False` — declared in the provider capability surface.

### 4.4 Optional belt-and-braces
- Add an `instructions`-level rule in `orchestrator.txt`: when calling `transfer_to_<agent>`, pass no arguments (the SDK accepts empty args). This avoids LLMs that try to invent handoff payloads we don't have schemas for.
- Cap `max_turns` slightly higher than `sentinel_max_tool_calls` — handoff calls count as turns and we're occasionally hitting the cap mid-handoff. Suggest `max_turns = max_tool_calls + 5`.

These four fixes are bundled into §7 step 1 (`install_sdk_defaults`) and step 4 (tools sweep).

---

## 5. Env-var surface

Additions to `.env` (no removals):

```bash
# Which provider drives all six agents this run.
SENTINEL_LLM_PROVIDER=groq          # groq | openai | gemini | anthropic

# Existing — kept.
GROQ_API_KEY=...
GROQ_BASE_URL=https://api.groq.com/openai/v1
GEMINI_API_KEY=...

# New.
OPENAI_API_KEY=                     # optional; enables tracing when set
ANTHROPIC_API_KEY=...
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/

# Per-agent model IDs — keep names, change defaults per provider.
SENTINEL_TRIAGE_MODEL=              # provider picks a sensible default if blank
SENTINEL_ANALYSIS_MODEL=
SENTINEL_JUDGE_MODEL=
```

Additions to `src/sentinel/config.py`:

```python
sentinel_llm_provider: Literal["openai", "groq", "gemini", "anthropic"] = "groq"
openai_api_key: str = ""
anthropic_api_key: str = ""
gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
# Make model-name fields default-empty so the provider can fill in:
sentinel_triage_model: str = ""
sentinel_analysis_model: str = ""
sentinel_judge_model: str = ""
```

Validators:
- If `sentinel_llm_provider == "groq"` → `groq_api_key` required (existing rule).
- If `"openai"` → `openai_api_key` required.
- If `"gemini"` → `gemini_api_key` required.
- If `"anthropic"` → `anthropic_api_key` required.
- A single `@model_validator(mode="after")` checks the active provider's key.

---

## 6. `providers/` package design

New tree under `src/sentinel/`:

```
providers/
├── __init__.py          # re-exports: ModelProvider, get_provider
├── base.py              # ModelProvider Protocol + ProviderCapabilities dataclass
├── factory.py           # get_provider(settings) → ModelProvider
├── openai_provider.py   # OpenAIProvider
├── groq_provider.py     # GroqProvider
├── gemini_provider.py   # GeminiProvider
└── anthropic_provider.py# AnthropicProvider
```

Tests mirror the source: `tests/test_providers/test_{openai,groq,gemini,anthropic,factory}.py`.

### 6.1 `base.py` — protocol + capabilities

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol
from agents import Model

@dataclass(frozen=True)
class ProviderCapabilities:
    supports_strict_schemas: bool      # OpenAI=True, Groq=False, Gemini=True, Anthropic=False
    supports_structured_outputs: bool  # OpenAI=True, Groq=True, Gemini=True, Anthropic=False
    supports_responses_api: bool       # OpenAI=True; else False

@dataclass(frozen=True)
class AgentModelDefaults:
    triage: str
    analysis: str
    judge: str

class ModelProvider(Protocol):
    name: str
    capabilities: ProviderCapabilities
    defaults: AgentModelDefaults

    def build_model(self, model_name: str) -> Model: ...
    def install_sdk_defaults(self) -> None: ...
    async def aclose(self) -> None: ...
```

`install_sdk_defaults()` is where each provider applies its one-shot fixes from §4: `set_default_openai_api("chat_completions")`, `set_tracing_disabled(True)` if no OpenAI key, etc. Called once during the FastAPI lifespan before any agent is built.

### 6.2 `groq_provider.py`

```python
from agents import set_default_openai_api, set_tracing_disabled
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

class GroqProvider:
    name = "groq"
    capabilities = ProviderCapabilities(
        supports_strict_schemas=False,
        supports_structured_outputs=True,
        supports_responses_api=False,
    )
    defaults = AgentModelDefaults(
        triage="llama-3.1-8b-instant",
        analysis="llama-3.3-70b-versatile",
        judge="llama-3.1-8b-instant",
    )

    def __init__(self, settings: Settings) -> None:
        self._client = AsyncOpenAI(api_key=settings.groq_api_key, base_url=settings.groq_base_url)
        self._settings = settings

    def install_sdk_defaults(self) -> None:
        set_default_openai_api("chat_completions")
        if not self._settings.openai_api_key:
            set_tracing_disabled(True)

    def build_model(self, model_name: str) -> Model:
        return OpenAIChatCompletionsModel(model=model_name, openai_client=self._client)

    async def aclose(self) -> None:
        await self._client.close()
```

### 6.3 `openai_provider.py`
Same shape — `AsyncOpenAI(api_key=settings.openai_api_key)` with no `base_url`. Capabilities all True. Defaults: `gpt-4o-mini` (triage/judge), `gpt-4o` (analysis). Does NOT disable tracing.

### 6.4 `gemini_provider.py`
Same shape as Groq — OpenAI-compatible endpoint. `AsyncOpenAI(api_key=settings.gemini_api_key, base_url=settings.gemini_base_url)`. Capabilities: `supports_strict_schemas=True`, structured=True, responses=False. Defaults: `gemini-2.5-flash` (triage/judge), `gemini-2.5-pro` (analysis). `install_sdk_defaults` pins chat-completions and disables tracing.

### 6.5 `anthropic_provider.py`

```python
from agents.extensions.models.litellm_model import LitellmModel  # extra: openai-agents[litellm]

class AnthropicProvider:
    name = "anthropic"
    capabilities = ProviderCapabilities(
        supports_strict_schemas=False,
        supports_structured_outputs=False,  # capability flag — see §6.6
        supports_responses_api=False,
    )
    defaults = AgentModelDefaults(
        triage="anthropic/claude-haiku-4-5",
        analysis="anthropic/claude-sonnet-4-6",
        judge="anthropic/claude-haiku-4-5",
    )

    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.anthropic_api_key
        self._settings = settings

    def install_sdk_defaults(self) -> None:
        set_default_openai_api("chat_completions")
        if not self._settings.openai_api_key:
            set_tracing_disabled(True)

    def build_model(self, model_name: str) -> Model:
        return LitellmModel(model=model_name, api_key=self._api_key)

    async def aclose(self) -> None:
        return None
```

### 6.6 Structured-output capability flag — auto-fallback wiring

Each `build_*_agent(...)` currently does `Agent(..., output_type=TriageResult, model=llm)`. We change the signature so it receives the `ModelProvider` instead of the raw client, and conditionally drops `output_type` when the provider can't honour it:

```python
def build_triage_agent(
    semantic_memory: SemanticMemory,
    episodic_memory: EpisodicMemory,
    *,
    provider: ModelProvider,
    model_name: str | None = None,
) -> Agent[TriageResult]:
    model_id = model_name or provider.defaults.triage
    llm = provider.build_model(model_id)
    kwargs: dict[str, Any] = {"model": llm}
    if provider.capabilities.supports_structured_outputs:
        kwargs["output_type"] = TriageResult
    return Agent(name=TRIAGE_AGENT_NAME, instructions=..., tools=..., **kwargs)
```

When `supports_structured_outputs=False`, the agent's prompt already requests a JSON structure (it does for triage today). We add a thin post-validation step in the orchestrator's run wrapper: if `result.final_output` is a `str`, attempt `TriageResult.model_validate_json(...)`; on failure, log and surface a `ToolError`-style structured failure. This is one helper (`coerce_output(text, schema)`) shared across agents — not a per-agent change.

For the orchestrator's own `output_type=IncidentSummary`, the same logic applies. The Anthropic case relies on the prompt to produce valid JSON; if we hit reliability issues we can pin Anthropic agents to `model_settings=ModelSettings(response_format={"type": "json_object"})` later.

### 6.7 `factory.py`

```python
def get_provider(settings: Settings) -> ModelProvider:
    match settings.sentinel_llm_provider:
        case "groq":      return GroqProvider(settings)
        case "openai":    return OpenAIProvider(settings)
        case "gemini":    return GeminiProvider(settings)
        case "anthropic": return AnthropicProvider(settings)
```

---

## 7. Implementation order

Each step is independently testable and reversible.

1. **`config.py` + `.env.example`** — add the four new keys + validator. Run existing tests to confirm no regressions.
2. **`providers/base.py` + `providers/factory.py`** — protocol, capability dataclass, factory stub that raises `NotImplementedError` for non-Groq providers.
3. **`providers/groq_provider.py`** — port the existing Groq wiring out of `main.py`. At this point the system runs Groq through the new abstraction with zero behaviour change. Run full test suite.
4. **Tools sweep** — add `strict_json_schema=False` to every `@function_tool` in `src/sentinel/tools/`. Verify by running the existing handoff test scenario; this alone should fix the handoff bug on Groq.
5. **Agent constructor migration** — change each `build_*_agent(..., groq_client, model_name)` to `build_*_agent(..., *, provider, model_name=None)`. Mechanical refactor — same six files, same diff shape. Update `main.py` lifespan to call `provider = get_provider(settings); provider.install_sdk_defaults()` and pass `provider` to each builder. Remove the inline `OpenAIChatCompletionsModel` construction from agent files entirely.
6. **`openai_provider.py`** — implement, add test. Sanity-check by setting `SENTINEL_LLM_PROVIDER=openai` locally with an OpenAI key.
7. **`gemini_provider.py`** — implement, add test. Capability `supports_structured_outputs=True`. The compat endpoint should let the test suite pass without modification.
8. **`anthropic_provider.py`** — add `openai-agents[litellm]` to `pyproject.toml`. Implement, add test. Wire the `coerce_output(text, schema)` helper into agent builders for `supports_structured_outputs=False`.
9. **`tests/test_providers/`** — one test per provider that asserts `build_model(...)` returns a `Model`, the capability flags are correct, and `install_sdk_defaults()` is idempotent. A single end-to-end test parametrised over all four providers (skipped via `pytest.mark.skipif(env-key-missing)`) runs the orchestrator against a canned scenario.

---

## 8. File-by-file change summary

| File | Change |
|---|---|
| `.env` / `.env.example` | Add `SENTINEL_LLM_PROVIDER`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_BASE_URL` |
| `pyproject.toml` | `openai-agents` → `openai-agents[litellm]` |
| `src/sentinel/config.py` | New fields + provider-key validator |
| `src/sentinel/providers/*` | New package, 6 files |
| `src/sentinel/agents/{orchestrator,triage,log_analyst,deploy_correlator,remediation,comms}.py` | Replace `groq_client: AsyncOpenAI` with `provider: ModelProvider`; remove inline `OpenAIChatCompletionsModel` import |
| `src/sentinel/agents/loader.py` or new `src/sentinel/agents/output_coercion.py` | Tiny `coerce_output(text, schema)` helper |
| `src/sentinel/main.py` | Replace Groq-specific lifespan section with `provider = get_provider(settings); provider.install_sdk_defaults()`; pass `provider` into every builder; add `await provider.aclose()` to shutdown |
| `src/sentinel/tools/*.py` | Add `strict_json_schema=False` to every `@function_tool(...)` decorator (consistent with §4.3) |
| `tests/test_providers/*` | New test module mirroring providers package |
| `tests/test_agents/*` | Replace `groq_client=AsyncOpenAI(...)` fixture usage with a `provider=FakeProvider()` fixture |
| `tests/conftest.py` | Add `fake_provider` fixture that returns a stub `ModelProvider` |
| `architecture.md` | Add §3.1 "Provider layer" and update §5 model identifiers to be provider-relative |

Lines of code changed: ~250 new (providers package + tests), ~120 modified (agents + main + tools).

---

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| LiteLLM is "best-effort/beta" per official docs — Anthropic tool-calls could regress | Pin `litellm>=1.x.y` known-good in `pyproject.toml`; integration test gated on `ANTHROPIC_API_KEY` env presence so CI doesn't break |
| Anthropic structured-output unreliability | Capability flag → plain-text + `coerce_output` validator with one retry. If still flaky, fall back to `tool_choice="required"` + a `submit_result` tool pattern (next plan, not this one) |
| Gemini OpenAI-compat endpoint occasionally redirects to Responses API in newer SDK versions | `install_sdk_defaults()` calls `set_default_openai_api("chat_completions")`; pin `openai<2.0` if needed |
| Increased test runtime if we hit live APIs | Default tests use a fake provider; live-API tests are explicitly opt-in via env-var skip |
| `output_key` / structured-output divergence breaks eval judge | Eval judge (`sentinel_judge_model`) lives outside the agent pipeline — keep it on Groq regardless of `SENTINEL_LLM_PROVIDER`, by reading `sentinel_judge_provider` (optional, defaults to the global) in a follow-up. Not in scope here |

---

## 10. Out of scope (explicit non-goals for this plan)

- Per-agent provider overrides (e.g. triage on Groq, analysis on Claude in the same run). The protocol supports this trivially later — `main.py` can call `get_provider(settings, override="anthropic")` per agent — but we ship the global switch first.
- Streaming token-level UI updates. Current dashboard works on event-bus messages, not token streams.
- Native Anthropic prompt-caching, Gemini thinking budgets. These need provider-specific `ModelSettings` extensions and are a follow-up plan once the abstraction is in.
- Replacing the existing `google-genai` import in eval. The eval module's Gemini path is independent of agent execution and stays where it is.

---

## 11. Acceptance criteria

The plan is "done" when:

1. `SENTINEL_LLM_PROVIDER=groq` runs `pytest -xvs` green (zero behaviour regression vs current main).
2. `SENTINEL_LLM_PROVIDER=openai|gemini|anthropic` each pass a smoke scenario (`scripts/run_scenario.py bad_deploy_01`) end-to-end with a successful handoff chain through all five specialists.
3. No `AsyncOpenAI` or `OpenAIChatCompletionsModel` import remains in `src/sentinel/agents/*.py`.
4. `ruff check src/ tests/` and `pyright src/` both clean.
5. `architecture.md` updated to describe the provider layer.
