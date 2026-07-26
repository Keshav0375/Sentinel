# task-4 — `fetch_logs` → Datadog Logs API   ·   [category-3-sentinel-backend / phase-3-tools]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `dev/backend-phase-3-tools` |
| **Commit prefix** | `refactor:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §3 Tools, §4.8, §13.2 |
| **Depends on** | [[task-1-config-extensions]] |
| **Referenced by** | analysis agent |

## Spec
Rewrite `fetch_logs` to call the Datadog Logs API via httpx (was synthetic).

**Files modified:** `src/sentinel/tools/log_fetcher.py`
- Input `LogQuery{service, window_minutes, level?}` → Output `LogAnalysis{entries, count, ...}`.
- httpx async call to Datadog Logs API using `dd_api_key`/`dd_app_key`/`dd_site` from config; retry w/ backoff (CLAUDE.md: 3 attempts 1/2/4s); read-only; structured `ToolError` on failure.
- Respect `SENTINEL_FAKE_LLM`/test mode by allowing an injected client for deterministic tests.

## Prerequisites
- [ ] task 3.1 config. [ ] ⛔ B6 Datadog keys for live calls (tests use a mock httpx client).

## Acceptance Criteria
- [ ] Real Datadog query shape correct (endpoint, auth headers, time window); returns typed entries.
- [ ] Retries/backoff on transient errors; structured error return (never raw raise).

## Tests
- **Unit (`tests/test_tools/test_log_fetcher.py`):** mocked httpx returns a canned Datadog response → parsed correctly; error path returns structured error; retry invoked on 5xx.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `pytest tests/test_tools/test_log_fetcher.py -q` green (mocked).
2. (with B6) a live query returns recent logs for `dummy-api`.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live calls ⛔ B6. Code + mocked tests writable now._
