# task-1 — `POST /webhooks/incident` receiver   ·   [category-3-sentinel-backend / phase-5-api]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel` (backend) |
| **Phase branch** | `impl/backend-phase-5-api` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel/ARCHITECTURE.md §3.1, §4.5 (concurrency), §2 (correlation) |
| **Depends on** | [[task-2-orchestrator-loops]], [[task-1-episodic-memory]], [[task-5-app-lifespan-and-concurrency]] (token auth) |
| **Referenced by** | [[task-3-ci-incident-response]] (caller), [[task-2-incident-query-endpoints]] (poll) |

> ⚠ **rev-5 (2026-07-12):** (a) auth is Entra bearer via `require_incident_write` (task 5.6),
> **not** `X-Sentinel-Token`; (b) the payload carries **`signal_type`** (`deploy_failure` |
> `runtime_error`) which the orchestrator branches on — deploy_failure → rollback fast path,
> runtime_error → full pipeline. See sentinel §3.1.

## Spec
The webhook that triggers the pipeline. Accepts the GHA-enriched payload, launches an
isolated asyncio task per incident, returns 202 immediately.

**Files created/modified:** `src/sentinel/api/webhooks.py`
- `IncidentPayload` Pydantic model per §3.1 (source, alert_id, dd_event_id, correlation_id, **signal_type**, title, severity, tags{service, deploy_status, failed_stage, version}, context{service_metadata, pr_details, recent_logs}, timestamp, raw_payload).
- `POST /webhooks/incident` (dep: `require_incident_write`) → generate `incident_id`, spawn `handle_incident` task with its own ShortTermMemory + LangFuse trace (§4.5), passing `signal_type` for the two-case branch, return 202 `{incident_id, correlation_id, status:"accepted"}`.
- Store terminal incident to episodic memory on completion.

## Prerequisites
- [ ] Phase-4 orchestrator, episodic memory, token auth (5.5). [ ] fake LLM for tests.

## Acceptance Criteria
- [ ] 202 with incident_id; pipeline runs as an independent task (concurrent incidents isolated).
- [ ] 401 without a valid token; correlation_id echoed.
- [ ] Terminal incident persisted with trajectory + resolution_type.

## Tests
- **Integration (`tests/test_api/test_webhooks.py`, fake LLM):** POST canned payload → 202; poll shows stored incident; missing token → 401; two concurrent POSTs isolated.
- **Quality gate:** `--repo backend`.

## How to Verify (phase gate)
1. `pytest tests/test_api/test_webhooks.py -q` green.
2. `curl -H "X-Sentinel-Token: …" -d @sample.json .../webhooks/incident` → 202 then a stored incident.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none locally (fake LLM + local pgvector)._
