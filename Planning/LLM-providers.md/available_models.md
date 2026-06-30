# Sentinel — Available Models Reference

Model strings use the `provider/model` format (LiteLLM convention).
Set them via the three env vars below. Only the key for the active provider is required.

---

## Quick Swap Examples

Switch the full stack to Anthropic in one `.env` block:
```env
SENTINEL_TRIAGE_MODEL=anthropic/claude-haiku-4-5
SENTINEL_ANALYSIS_MODEL=anthropic/claude-sonnet-4-6
SENTINEL_JUDGE_MODEL=anthropic/claude-haiku-4-5
ANTHROPIC_API_KEY=sk-ant-...
```

Switch analysis to OpenAI, keep triage/judge on Groq:
```env
SENTINEL_ANALYSIS_MODEL=openai/gpt-4o
OPENAI_API_KEY=sk-...
# GROQ_API_KEY still required for triage + judge defaults
```

Keep everything on Groq (default — no extra keys needed):
```env
GROQ_API_KEY=gsk_...
# All three model fields default to groq/... — nothing else required
```

---

## Groq  (`groq/`)

**Required key:** `GROQ_API_KEY`  
**Free tier:** Yes — generous free limits, fastest cold-start latency.

| Model string | Role | Context | Notes |
|---|---|---|---|
| `groq/llama-3.3-70b-versatile` | analysis, orchestrator | 128 k | Default analysis model. Strong reasoning, tool use. |
| `groq/llama-3.1-8b-instant` | triage, judge | 128 k | Default triage + judge. Fast, low-cost. |
| `groq/llama-3.1-70b-versatile` | analysis | 128 k | Alternative to 3.3-70b. Slightly older. |
| `groq/gemma2-9b-it` | triage | 8 k | Fastest option for simple classification. |
| `groq/mixtral-8x7b-32768` | analysis | 32 k | MoE architecture; good for long log dumps. |

**Example `.env`:**
```env
GROQ_API_KEY=gsk_...
SENTINEL_TRIAGE_MODEL=groq/llama-3.1-8b-instant
SENTINEL_ANALYSIS_MODEL=groq/llama-3.3-70b-versatile
SENTINEL_JUDGE_MODEL=groq/llama-3.1-8b-instant
```

---

## OpenAI  (`openai/`)

**Required key:** `OPENAI_API_KEY`  
**Structured outputs:** Yes — `output_type=` is set on all agents.  
**Strict schemas:** Yes — tool schemas use strict validation.

| Model string | Role | Context | Notes |
|---|---|---|---|
| `openai/gpt-4o` | analysis, orchestrator | 128 k | Best reasoning + structured output fidelity. |
| `openai/gpt-4o-mini` | triage, judge | 128 k | Fast + cheap. Equivalent to Groq llama-3.1-8b in cost tier. |
| `openai/o1-mini` | analysis | 128 k | Reasoning model — slow but thorough for complex root-cause. |

**Example `.env`:**
```env
OPENAI_API_KEY=sk-...
SENTINEL_TRIAGE_MODEL=openai/gpt-4o-mini
SENTINEL_ANALYSIS_MODEL=openai/gpt-4o
SENTINEL_JUDGE_MODEL=openai/gpt-4o-mini
```

---

## Anthropic  (`anthropic/`)

**Required key:** `ANTHROPIC_API_KEY`  
**Structured outputs:** No — agents return plain text; `coerce_output()` parses JSON.  
**Strict schemas:** No — tool schemas use permissive validation.

| Model string | Role | Context | Notes |
|---|---|---|---|
| `anthropic/claude-sonnet-4-6` | analysis, orchestrator | 200 k | Best balance of quality and speed in the Claude 4 family. |
| `anthropic/claude-haiku-4-5` | triage, judge | 200 k | Fast, low-cost. Replaces Haiku 3.5 in the 4.x line. |
| `anthropic/claude-opus-4-6` | analysis | 200 k | Highest quality; slowest + most expensive. |

> **Note on structured outputs:** Anthropic models don't support OpenAI-style JSON mode.
> When using an `anthropic/` model, agents return plain text and Sentinel's
> `coerce_output()` fallback strips markdown fences and calls `model_validate_json`.
> If the model returns malformed JSON, the field will be `None` and the orchestrator
> will escalate.

**Example `.env`:**
```env
ANTHROPIC_API_KEY=sk-ant-...
SENTINEL_TRIAGE_MODEL=anthropic/claude-haiku-4-5
SENTINEL_ANALYSIS_MODEL=anthropic/claude-sonnet-4-6
SENTINEL_JUDGE_MODEL=anthropic/claude-haiku-4-5
```

---

## Azure OpenAI  (`azure/`)

**Required keys:** `AZURE_API_KEY`, `AZURE_API_BASE`  
**Optional:** `AZURE_API_VERSION` (default: `2024-02-01`)  
**Structured outputs:** Yes — same as direct OpenAI.  
**Strict schemas:** Yes.

> The model name after `azure/` must match the **deployment name** in your Azure resource,
> not the base model name. E.g. if you deployed `gpt-4o` as `"my-gpt4o"`, use
> `azure/my-gpt4o`.

| Model string | Role | Context | Notes |
|---|---|---|---|
| `azure/<your-gpt4o-deployment>` | analysis, orchestrator | 128 k | GPT-4o via Azure. Enterprise SLA + VNet. |
| `azure/<your-gpt4o-mini-deployment>` | triage, judge | 128 k | GPT-4o-mini via Azure. |

**Example `.env`:**
```env
AZURE_API_KEY=...
AZURE_API_BASE=https://my-resource.openai.azure.com/
AZURE_API_VERSION=2024-02-01
SENTINEL_TRIAGE_MODEL=azure/my-gpt4o-mini
SENTINEL_ANALYSIS_MODEL=azure/my-gpt4o
SENTINEL_JUDGE_MODEL=azure/my-gpt4o-mini
```

---

## Provider Capability Summary

| Provider | Structured outputs | Strict schemas | Free tier | Best for |
|---|---|---|---|---|
| `groq/` | ❌ | ❌ | ✅ | Dev + cost-sensitive prod |
| `openai/` | ✅ | ✅ | ❌ | Highest output reliability |
| `anthropic/` | ❌ | ❌ | ❌ | Long context, nuanced reasoning |
| `azure/` | ✅ | ✅ | ❌ | Enterprise / VNet / compliance |

**Gemini:** Excluded from the current provider layer. Will be added via Google ADK
integration in a future task.

---

## Per-Agent Roles

| Env var | Default | Purpose |
|---|---|---|
| `SENTINEL_TRIAGE_MODEL` | `groq/llama-3.1-8b-instant` | Fast classification — severity, service, dedup |
| `SENTINEL_ANALYSIS_MODEL` | `groq/llama-3.3-70b-versatile` | Log analysis, deploy correlation, remediation drafting, orchestration |
| `SENTINEL_JUDGE_MODEL` | `groq/llama-3.1-8b-instant` | Trajectory eval judge — scored after incident resolves |

The same `SENTINEL_ANALYSIS_MODEL` string is used for the Log Analyst, Deploy Correlator,
Remediation Agent, and Orchestrator. If you want per-agent control, that's a future
enhancement (see `src/sentinel/providers/capabilities.py` for the flag table).
