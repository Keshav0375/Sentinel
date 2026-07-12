# task-1 — `config.py` extensions   ·   [category-3-sentinel-backend / phase-3-tools]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `impl/backend-phase-3-tools` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §4.7, §13.2 (config), §8.2; env-examples/backend.env.example |
| **Depends on** | [[task-1-phase2-dependencies]] |
| **Referenced by** | every tool, provider routing, tracing, database, api |

## Spec
Extend `Settings` (pydantic-settings) with all Phase-2 config keys.

**Files modified:** `src/sentinel/config.py`
- LLM: `sentinel_primary_provider`, `anthropic_api_key`, `openai_api_key`.
- DB: `database_url`, `db_pool_min`, `db_pool_max`.
- LangFuse: `langfuse_public_key`, `langfuse_secret_key`, `langfuse_host`.
- Datadog: `dd_api_key`, `dd_app_key`, `dd_site`.
- GitHub: `github_api_token`.
- API auth: `sentinel_api_token`.
- Runtime: `log_level`, `max_tool_calls` (default 20), `embedding_model`, `fake_llm` (bool).
- All typed, env-driven, `SENTINEL_`-prefixed where applicable; secrets as `SecretStr`.

## Prerequisites
- [ ] task 1.1 deps.

## Acceptance Criteria
- [ ] Settings load from env/.env; `max_tool_calls` defaults to 20; secrets are `SecretStr`.
- [ ] `.env.example` (repo root) matches the fields (already reconstructed).
- [ ] Missing required secret raises a clear validation error (not a silent None) in non-fake mode.

## Tests
- **Unit (`tests/test_config.py`):** loads a sample env; defaults correct; `SENTINEL_FAKE_LLM=1` relaxes required-key validation.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `pytest tests/test_config.py -q` green.
2. `python -c "from sentinel.config import get_settings; print(get_settings().max_tool_calls)"` → 20.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none locally._
