# Sentinel — TODO Tracker

> Each task is sized at 1-5% of total project effort.
> Check off tasks as you complete them. Add notes/blockers inline.
> **Total: 100% = Interview-ready MVP with eval + demo**

---

## Phase 0 — Repo Scaffold & Config (0-8%)

### [x] 0.1 — Initialize Python project with `pyproject.toml` (2%)
Set up the project with `pyproject.toml` (not `setup.py`). Use `hatchling` or `setuptools` as build backend. Define dependencies:
- `openai-agents` (Agents SDK)
- `openai` (embeddings + direct calls)
- `fastapi`, `uvicorn`
- `pydantic`, `pydantic-settings`
- `aiosqlite` (async SQLite)
- `numpy` (cosine similarity)
- `httpx` (async HTTP)
- `python-dotenv`
- Dev deps: `pytest`, `pytest-asyncio`, `ruff`, `pyright`

Create `.python-version` (3.12+). Create `src/sentinel/__init__.py`.
```
Notes:
─────
2026-05-10: Created pyproject.toml (hatchling backend), .python-version (3.12), src/sentinel/__init__.py. Added google-genai, structlog, sentence-transformers per ARCHITECTURE.md. Pyright strict mode uses array syntax.
```

### [x] 0.2 — Create full directory structure (1%)
Create every directory and `__init__.py` from ARCHITECTURE.md §4. Don't write logic yet — just empty files with module docstrings. This gives Claude Code the full map.
```
Notes:
─────
2026-05-10: Created all 9 packages (api, models, agents, tools, memory, eval, generator, infra + prompts dir), tests/test_*, scripts/, data/scenarios/, data/services/, reports/. Every .py stub has from __future__ import annotations + module docstring.
```

### [x] 0.3 — Config module with `pydantic-settings` (2%)
`src/sentinel/config.py` — Load all config from env vars with sensible defaults:
- `OPENAI_API_KEY` (required)
- `SENTINEL_DB_PATH` (default: `./data/sentinel.db`)
- `SENTINEL_LOG_LEVEL` (default: `INFO`)
- `SENTINEL_MAX_TOOL_CALLS` (default: `15`)
- `SENTINEL_EMBEDDING_MODEL` (default: `text-embedding-3-small`)
- `SENTINEL_TRIAGE_MODEL` (default: `gpt-4o-mini`)
- `SENTINEL_ANALYSIS_MODEL` (default: `gpt-4o`)
- `SENTINEL_JUDGE_MODEL` (default: `gpt-4o-mini`)

Create `.env.example` with all vars documented.
```
Notes:
─────
2026-05-10: Used GROQ_API_KEY (not OPENAI_API_KEY) and Groq model names per ARCHITECTURE.md. Embedding model is all-MiniLM-L6-v2 (local). Added Literal type for log_level, Path for db_path, validators for key/tool-call bounds. Added pythonpath=["src"] to pytest config. 5/5 tests pass.
```

### [ ] 0.4 — Docker + docker-compose + .gitignore (1%)
`Dockerfile` — multi-stage, Python 3.12-slim, copies `src/`, installs from `pyproject.toml`.
`docker-compose.yml` — single service for now, mounts `.env`, exposes port 8000.
`.gitignore` — Python defaults + `.env` + `data/*.db` + `__pycache__`.
```
Notes:
─────
```

### [ ] 0.5 — Structured logging + basic infra (2%)
`src/sentinel/infra/logging.py` — `structlog` or stdlib `logging` with JSON formatter. Every log line includes `incident_id` when in incident context.
`src/sentinel/infra/db.py` — async SQLite connection factory using `aiosqlite`. Context manager pattern. Create tables on first run (migration-free for MVP).
```
Notes:
─────
```

---

## Phase 1 — Pydantic Domain Models (8-16%)

### [ ] 1.1 — Alert models (2%)
`src/sentinel/models/alert.py`:
- `AlertSource` (enum: `datadog`, `pagerduty`)
- `AlertPayload` — source, service, metric, threshold, current_value, severity, timestamp, alert_id, metadata dict
- `AlertAck` — alert_id, received_at, incident_id (assigned by receiver)

These are the contract between webhook receiver and orchestrator.
```
Notes:
─────
```

### [ ] 1.2 — Incident + Service models (2%)
`src/sentinel/models/incident.py`:
- `Severity` (enum: P1-P4)
- `IncidentStatus` (enum: `triage`, `investigating`, `remediating`, `pending_approval`, `resolved`, `escalated`)
- `TimelineEntry` — timestamp, agent_name, action, result_summary
- `Incident` — id, alert, status, severity, affected_service, timeline list, triage_result, log_analysis, deploy_correlation, remediation_plan, comms_summary, created_at, resolved_at

`src/sentinel/models/service.py`:
- `ServiceMetadata` — name, team, tier, oncall_channel, repo_url, description, dependencies list
```
Notes:
─────
```

### [ ] 1.3 — Log, Deploy, Remediation models (2%)
`src/sentinel/models/log_entry.py`:
- `LogEntry` — timestamp, level, service, message, trace_id (optional)
- `LogQuery` — service, start_time, end_time, level_filter, keyword_filter
- `LogAnalysis` — error_patterns list, anomaly_summary, key_log_lines list, hypothesis

`src/sentinel/models/deploy.py`:
- `Deploy` — id, service, timestamp, author, commit_sha, files_changed, description
- `DeployCorrelation` — recent_deploys list, suspect_deploy, confidence float, evidence string

`src/sentinel/models/remediation.py`:
- `RemediationAction` (enum: `rollback`, `hotfix`, `scale`, `restart`, `escalate`)
- `RemediationPlan` — action_type, deploy_id (if rollback), diff (if hotfix), risk_assessment, justification
- `RollbackPR` — title, body, deploy_id, target_branch
- `ApprovalRequest` — action description, risk_level, evidence_summary, proposed_by agent name
- `ApprovalResult` — status (approved/rejected/timeout), reviewer, comment
```
Notes:
─────
```

### [ ] 1.4 — Memory + Eval models (2%)
`src/sentinel/models/memory.py`:
- `EpisodicRecord` — id, service_name, severity, symptoms, root_cause, resolution, mttr_seconds, embedding (list[float]), created_at
- `SemanticRecord` — service metadata + runbook reference
- `MemoryQueryResult` — records list, similarity_scores list

`src/sentinel/models/eval_result.py`:
- `EvalDimension` (enum: triage_accuracy, root_cause_correctness, tool_efficiency, mttr, remediation_safety, comms_quality)
- `DimensionScore` — dimension, score (0-5), reasoning
- `TrajectoryScore` — incident_id, scenario_id, dimension_scores list, total_score, judge_model
```
Notes:
─────
```

---

## Phase 2 — Synthetic Data Generator (16-28%)

### [ ] 2.1 — Scenario schema + first 3 scenarios (3%)
`data/scenarios/` — Create JSON files for:
1. `bad_deploy_01.json` — null pointer from missing config key
2. `bad_deploy_02.json` — dependency version mismatch
3. `db_pool_01.json` — connection pool exhaustion

Each file has: scenario_id, failure_class, description, alert payload, known_root_cause, logs array, deploys array. Follow the schema from ARCHITECTURE.md §9.
```
Notes:
─────
```

### [ ] 2.2 — Remaining 7 scenarios (3%)
Create scenarios 4-10:
4. `bad_deploy_03.json` — missing env var
5. `db_pool_02.json` — connection leak
6. `downstream_outage_01.json` — third-party API down
7. `downstream_outage_02.json` — internal service timeout cascade
8. `memory_leak_01.json` — OOM from unbounded cache
9. `memory_leak_02.json` — event listener accumulation
10. `config_regression_01.json` — feature flag breaks subset

Make logs realistic — include timestamps, proper log levels, stack traces where appropriate, red herrings mixed with signal.
```
Notes:
─────
```

### [ ] 2.3 — Service map + dependency graph seed data (2%)
`data/services/service_map.json` — Define 6-8 fake services:
- `api-gateway` (critical, team-platform)
- `user-service` (critical, team-identity)
- `payment-service` (critical, team-payments)
- `notification-service` (standard, team-comms)
- `analytics-pipeline` (best-effort, team-data)
- `auth-service` (critical, team-identity)
- `order-service` (critical, team-commerce)
- `cdn-proxy` (standard, team-platform)

`data/services/dependency_graph.json` — Define edges: api-gateway → [user-service, payment-service, auth-service], order-service → [payment-service, notification-service], etc.

Include oncall channels, repo URLs (fake GitHub URLs), runbook references.
```
Notes:
─────
```

### [ ] 2.4 — Alert generator module (2%)
`src/sentinel/generator/alert_gen.py`:
- `load_scenario(scenario_id: str) -> Scenario` — loads from JSON
- `generate_alert(scenario: Scenario) -> AlertPayload` — creates the alert webhook payload
- `list_scenarios() -> list[str]` — returns available scenario IDs

`src/sentinel/generator/scenarios.py`:
- `Scenario` Pydantic model matching the JSON schema
- Loader with validation
```
Notes:
─────
```

### [ ] 2.5 — Log + deploy generator modules (2%)
`src/sentinel/generator/log_gen.py`:
- `generate_logs(scenario: Scenario, noise: bool = True) -> list[LogEntry]` — returns scenario logs + optional noise (unrelated info-level logs from other services to make it realistic)

`src/sentinel/generator/deploy_gen.py`:
- `generate_deploys(scenario: Scenario) -> list[Deploy]` — returns deploy history, including the culprit + innocent deploys as distractors
```
Notes:
─────
```

### [ ] 2.6 — Seed data loader (seed.py) (1%)
`data/seed.py` — Script that:
1. Creates SQLite DB at configured path
2. Runs CREATE TABLE statements for semantic memory (services, runbooks)
3. Inserts service map + dependency graph from JSON files
4. Inserts runbooks for each service/failure_class combo

Run with: `python -m data.seed`
```
Notes:
─────
```

---

## Phase 3 — Memory Subsystem (28-38%)

### [ ] 3.1 — Memory store protocol/ABC (1%)
`src/sentinel/memory/base.py`:
- `MemoryStore` protocol with abstract methods: `store()`, `query()`, `get()`, `delete()`
- Keeps memory implementations swappable (SQLite now, Cosmos later)
```
Notes:
─────
```

### [ ] 3.2 — Embedding client wrapper (2%)
`src/sentinel/memory/embeddings.py`:
- `EmbeddingClient` class wrapping OpenAI `text-embedding-3-small`
- `async embed(text: str) -> list[float]`
- `async embed_batch(texts: list[str]) -> list[list[float]]`
- Caching layer (simple dict cache to avoid re-embedding identical strings)
```
Notes:
─────
```

### [ ] 3.3 — Semantic memory (SQLite) (2%)
`src/sentinel/memory/semantic.py`:
- `SemanticMemory` class implementing `MemoryStore`
- `get_service(name: str) -> ServiceMetadata`
- `get_dependencies(name: str) -> list[str]`
- `get_runbook(service: str, failure_class: str) -> Runbook | None`
- Loads from SQLite `services` and `runbooks` tables
- Cached in-memory after first load per incident
```
Notes:
─────
```

### [ ] 3.4 — Episodic memory (SQLite + embeddings) (3%)
`src/sentinel/memory/episodic.py`:
- `EpisodicMemory` class implementing `MemoryStore`
- `store_incident(incident: Incident) -> None` — embed symptoms, store full record
- `search_similar(symptoms: str, top_k: int = 5) -> list[EpisodicRecord]` — embed query, cosine similarity against all stored embeddings, return top-k
- `get_incident(id: str) -> EpisodicRecord | None`
- Cosine similarity: `numpy.dot(a, b) / (norm(a) * norm(b))`

This is the key learning feature. Test it: store 3 incidents, query with similar symptoms, verify correct ranking.
```
Notes:
─────
```

### [ ] 3.5 — Short-term memory (in-memory) (2%)
`src/sentinel/memory/short_term.py`:
- `ShortTermMemory` class — Python dict keyed by `incident_id`
- `create(incident_id: str, alert: AlertPayload) -> None`
- `add_timeline_entry(incident_id: str, entry: TimelineEntry) -> None`
- `get_timeline(incident_id: str) -> list[TimelineEntry]`
- `get_context(incident_id: str) -> dict` — returns full incident state for agent context
- `clear(incident_id: str) -> None`

Every tool call and handoff gets logged here. This is what the eval system reads.
```
Notes:
─────
```

---

## Phase 4 — Tool Layer (38-50%)

### [ ] 4.1 — Tool registry pattern (1%)
`src/sentinel/tools/registry.py`:
- Central list of all tools
- Each tool is a standalone function decorated with `@function_tool`
- Tools receive injected dependencies (memory clients, generators) via closure or class
```
Notes:
─────
```

### [ ] 4.2 — `get_service_metadata` tool (2%)
`src/sentinel/tools/service_lookup.py`:
- Takes: service name (str)
- Returns: ServiceMetadata (team, tier, deps, oncall, runbook summary)
- Reads from semantic memory
- Used by: Triage Agent, Log Analyst
```
Notes:
─────
```

### [ ] 4.3 — `fetch_logs` tool (2%)
`src/sentinel/tools/log_fetcher.py`:
- Takes: LogQuery (service, time range, level filter)
- Returns: list of LogEntry objects
- In MVP: reads from loaded scenario data (the generator), not a real log API
- Adds noise logs if configured, to test agent's ability to filter signal from noise
```
Notes:
─────
```

### [ ] 4.4 — `list_recent_deploys` tool (2%)
`src/sentinel/tools/deploy_checker.py`:
- Takes: service name, hours_back (default 2)
- Returns: list of Deploy objects sorted by timestamp desc
- In MVP: reads from scenario data
```
Notes:
─────
```

### [ ] 4.5 — `search_past_incidents` tool (2%)
`src/sentinel/tools/incident_search.py`:
- Takes: symptom_query (str), top_k (int, default 5)
- Returns: list of EpisodicRecord with similarity scores
- Calls episodic memory's search_similar
- This is the tool that makes the agent learn from history
```
Notes:
─────
```

### [ ] 4.6 — `draft_rollback_pr` + `draft_hotfix` tools (2%)
`src/sentinel/tools/remediation_tools.py`:
- `draft_rollback_pr`: Takes deploy_id + justification → Returns RollbackPR (title, body, target branch)
- `draft_hotfix`: Takes file_path + fix_description → Returns HotfixPatch (diff string, test suggestions)
- These produce artifacts, not actions. The remediation agent uses the output to populate the HITL approval request.
```
Notes:
─────
```

### [ ] 4.7 — `draft_slack_summary` tool (1%)
`src/sentinel/tools/comms_tools.py`:
- Takes: full incident timeline dict
- Returns: formatted Slack summary string (Impact / Root Cause / Timeline / Status / Action Items / ETA)
- Template-driven formatting
```
Notes:
─────
```

### [ ] 4.8 — `request_human_approval` HITL gate (2%)
`src/sentinel/tools/hitl.py`:
- Takes: ApprovalRequest (action, risk_level, evidence_summary, proposed_by)
- MVP: prints the request to terminal, waits for `input()` — `approve` or `reject`
- Returns: ApprovalResult
- This is the **critical safety boundary**. Log every approval/rejection.
- Later: swap implementation to Slack interactive button via DI
```
Notes:
─────
```

---

## Phase 5 — Agent Definitions (50-68%)

### [ ] 5.1 — System prompt files (2%)
Create `src/sentinel/agents/prompts/` directory with text files:
- `orchestrator.txt` — role, handoff rules, tool-call cap, incident workflow
- `triage.txt` — classification rules, severity definitions, dedup logic
- `log_analyst.txt` — log analysis methodology, pattern recognition focus
- `deploy_correlator.txt` — correlation heuristics, suspicion ranking criteria
- `remediation.txt` — rollback vs hotfix decision tree, HITL requirement
- `comms.txt` — Slack summary format template, tone guidelines

System prompts are loaded from files, not hardcoded strings. This makes prompt iteration easy (change file, restart).
```
Notes:
─────
```

### [ ] 5.2 — Triage Agent (3%)
`src/sentinel/agents/triage.py`:
- Define Agent with name, instructions (from prompt file), tools (`get_service_metadata`, `search_past_incidents`), model (`gpt-4o-mini`)
- Output: the agent should produce a `TriageResult` (severity, service, is_duplicate, recommended_action)
- Test standalone: fire a scenario alert → verify correct severity + service identification
```
Notes:
─────
```

### [ ] 5.3 — Log Analyst Agent (3%)
`src/sentinel/agents/log_analyst.py`:
- Tools: `fetch_logs`, `get_service_metadata`
- Model: `gpt-4o`
- Agent should: fetch logs for the identified service, find error patterns, produce a root cause hypothesis
- Test: given a bad_deploy scenario, does it identify the error signature in logs?
```
Notes:
─────
```

### [ ] 5.4 — Deploy Correlator Agent (2%)
`src/sentinel/agents/deploy_correlator.py`:
- Tools: `list_recent_deploys`
- Model: `gpt-4o-mini`
- Agent should: list deploys, rank by suspicion (timing proximity + change scope), identify the prime suspect
- Test: given 3 deploys where one is the culprit, does it correctly rank?
```
Notes:
─────
```

### [ ] 5.5 — Remediation Agent (3%)
`src/sentinel/agents/remediation.py`:
- Tools: `draft_rollback_pr`, `draft_hotfix`, `request_human_approval`
- Model: `gpt-4o`
- Agent should: decide rollback vs hotfix based on evidence, draft the artifact, request HITL approval
- **Critical test:** does it ALWAYS call `request_human_approval` before finalizing? If it ever skips the gate, the system prompt needs fixing.
```
Notes:
─────
```

### [ ] 5.6 — Comms Agent (2%)
`src/sentinel/agents/comms.py`:
- Tools: `draft_slack_summary`
- Model: `gpt-4o-mini`
- Agent should: take full incident context, produce a clean Slack summary
- Test: verify output follows the format template (Impact/Root Cause/Timeline/Status/Actions/ETA)
```
Notes:
─────
```

### [ ] 5.7 — Orchestrator Agent + Handoff Wiring (3%)
`src/sentinel/agents/orchestrator.py`:
- This is the top-level agent that receives the alert and coordinates everything
- Handoffs: `[triage_agent, log_analyst, deploy_correlator, remediation_agent, comms_agent]`
- Model: `gpt-4o`
- System prompt defines the workflow order and handoff conditions
- Implements tool-call cap (15): if exceeded, auto-escalate
- Test: fire a full scenario → verify the orchestrator calls agents in the right order
```
Notes:
─────
```

---

## Phase 6 — API + End-to-End Pipeline (68-78%)

### [ ] 6.1 — FastAPI webhook receiver (2%)
`src/sentinel/api/webhooks.py`:
- `POST /webhooks/alert` — accepts AlertPayload, assigns incident_id, triggers orchestrator
- Idempotency: check alert_id, skip if already processed (dedup dict, 60s TTL)
- Returns 202 Accepted with incident_id
```
Notes:
─────
```

### [ ] 6.2 — Incident query endpoint (1%)
`src/sentinel/api/incidents.py`:
- `GET /incidents` — list recent incidents (from short-term memory)
- `GET /incidents/{id}` — get full incident timeline
- `GET /health` — basic health check
```
Notes:
─────
```

### [ ] 6.3 — FastAPI app entrypoint + lifespan (2%)
`src/sentinel/main.py`:
- Create FastAPI app with lifespan handler
- On startup: init DB, seed if needed, create memory clients, create agent instances (DI)
- Mount routers from api/
- Run with: `uvicorn sentinel.main:app --reload`
```
Notes:
─────
```

### [ ] 6.4 — Tracing/trajectory capture (2%)
`src/sentinel/infra/tracing.py`:
- Capture every agent step: tool calls, handoffs, LLM responses
- Store as structured timeline in short-term memory
- Hook into Agents SDK's built-in tracing (it has `@trace` support)
- Output: a JSON trajectory file per incident
```
Notes:
─────
```

### [ ] 6.5 — End-to-end smoke test (3%)
`scripts/run_scenario.py`:
- CLI script: `python scripts/run_scenario.py bad_deploy_01`
- Loads scenario → generates alert → POSTs to webhook → waits for resolution
- Prints full timeline + HITL prompts
- This is your "does it work" checkpoint. Don't proceed to eval until 3+ scenarios pass cleanly.
```
Notes:
─────
```

### [ ] 6.6 — SSE event bus for real-time pipeline updates (3%)
`src/sentinel/infra/event_bus.py`:
- `EventBus` class using `asyncio.Queue` — one queue per connected client
- Event types: `agent_started`, `agent_completed`, `tool_called`, `tool_result`, `hitl_requested`, `hitl_resolved`, `incident_resolved`
- Each event is a Pydantic model: `PipelineEvent(type, agent_name, timestamp, data)`
- Orchestrator emits events at each step: before/after handoff, before/after tool call, on HITL gate
- This is the bridge between agent pipeline and dashboard — agents push events, SSE endpoint streams them
```
Notes:
─────
```

### [ ] 6.7 — SSE streaming endpoint + HITL approval API (2%)
`src/sentinel/api/events.py`:
- `GET /events/{incident_id}` — SSE endpoint, yields `PipelineEvent` as `text/event-stream`
- Uses `EventBus` to subscribe to events for a specific incident
- `POST /incidents/{incident_id}/approve` — body: `{"action": "approve" | "reject", "comment": "optional"}`
- On approve/reject: resolves the HITL gate (sets an `asyncio.Event` that the `request_human_approval` tool awaits)
- This replaces the `input()` HITL implementation when dashboard is active — tool checks if running in dashboard mode vs CLI mode
```
Notes:
─────
```

### [ ] 6.8 — Live incident dashboard (single HTML file) (4%)
`src/sentinel/dashboard/index.html`:
- Served by FastAPI at `GET /` via `StaticFiles` or inline route
- No framework — vanilla HTML + JS + CSS using `EventSource` API for SSE
- Layout matches the mockup: incident header → agent pipeline cards → metrics bar
- Agent cards start grayed out, light up as `agent_started` events arrive, show results on `agent_completed`
- Tool calls appear as sub-items under the active agent card
- HITL card highlights with warning border + approve/reject buttons when `hitl_requested` fires
- Approve/reject buttons POST to `/incidents/{id}/approve`
- Bottom metrics bar updates live: tool call count, elapsed time, tokens used, memory hits
- Responsive — works on a Loom recording at any window size
- Add a "Fire scenario" dropdown at the top: select scenario → POST to `/webhooks/alert` → dashboard starts streaming
```
Notes:
─────
```

### [ ] 6.9 — Eval results display page (2%)
`src/sentinel/dashboard/eval.html`:
- Served at `GET /eval`
- Shows latest eval report: per-scenario scores, per-dimension averages, pass/fail
- Reads from `reports/eval_report.json` (generated by eval runner)
- Simple table + color-coded score cells (green ≥4, amber ≥2.5, red <2.5)
- Link from main dashboard: "View eval results →"
- This is the second Loom moment: "here's how I grade the agent's performance"
```
Notes:
─────
```

---

## Phase 7 — Trajectory Eval (78-90%)

### [ ] 7.1 — Eval rubric + judge prompts (2%)
`src/sentinel/eval/rubric.py`:
- Define the 6 eval dimensions as enum
- Write judge system prompt: given a trajectory JSON + scenario ground truth, score each dimension 0-5 with reasoning
- Structured output: the judge returns a `TrajectoryScore` Pydantic model
```
Notes:
─────
```

### [ ] 7.2 — LLM-as-judge implementation (3%)
`src/sentinel/eval/judge.py`:
- `evaluate_trajectory(trajectory: dict, ground_truth: dict) -> TrajectoryScore`
- Calls the judge model with the rubric prompt
- Parses structured output into TrajectoryScore
- Uses `gpt-4o-mini` as judge (different family if you later add Claude for analysis)
```
Notes:
─────
```

### [ ] 7.3 — Eval runner (batch scenarios) (3%)
`src/sentinel/eval/runner.py` + `scripts/run_eval.py`:
- Iterates over all 10 scenarios (or a subset)
- For each: generate alert → run pipeline → capture trajectory → judge it
- Collects all TrajectoryScores
- Handles failures gracefully (scenario fails → score 0, log error, continue)
```
Notes:
─────
```

### [ ] 7.4 — Eval report generation (2%)
`src/sentinel/eval/report.py`:
- Takes list of TrajectoryScores → produces:
  1. JSON report (machine-readable, for CI gates later)
  2. Markdown summary (human-readable)
- Per-dimension averages, per-scenario breakdown, pass/fail thresholds
- Output to `reports/eval_report.md` and `reports/eval_report.json`
```
Notes:
─────
```

---

## Phase 8 — Polish + Demo (90-100%)

### [ ] 8.1 — README.md (3%)
Write the public-facing README:
- Project title + one-line description
- Architecture diagram (Mermaid or ASCII)
- Features list (what it does)
- Quick start (clone, .env, docker-compose up, fire a scenario)
- Demo section (link to Loom or GIF)
- Tech stack table
- Eval results summary
- Phase 2 roadmap teaser
```
Notes:
─────
```

### [ ] 8.2 — Demo script + recording (3%)
`scripts/demo.py`:
- Interactive demo runner with rich terminal output (use `rich` library)
- Walks through: alert → triage → logs → deploy → remediation → HITL → resolution
- Shows agent thinking, tool calls, handoffs in real-time
- Record a 2-3 minute Loom walking through the demo
```
Notes:
─────
```

### [ ] 8.3 — GHA CI pipeline (2%)
`.github/workflows/ci.yml`:
- Lint (ruff)
- Type check (pyright)
- Unit tests (pytest)
- No eval gate for MVP (would need OpenAI API key in CI), but add a placeholder job
```
Notes:
─────
```

### [ ] 8.4 — Final cleanup + edge cases (2%)
- Error handling: what happens when OpenAI API fails mid-incident? (retry with backoff)
- Tool-call cap enforcement: verify the orchestrator stops at 15 calls
- Memory persistence: verify episodic memory survives process restart
- Type checking: run pyright, fix all errors
- Remove dead code, unused imports
- Verify all 10 scenarios pass eval with score > 3.0 average
```
Notes:
─────
```

---

## Phase 2 Roadmap (Post-MVP, not tracked here)

These are documented for interview conversations ("what would you do next"):
- [ ] Azure Container Apps deployment (Bicep IaC)
- [ ] Cosmos DB (vector + JSON) replacing SQLite
- [ ] Redis for short-term memory
- [ ] LiteLLM + Kong AI Gateway for model routing + token budgets
- [ ] Self-hosted LangFuse for observability
- [ ] Slack interactive buttons for HITL (real Slack app)
- [ ] GitHub App for PR creation (real GitHub integration)
- [ ] Self-improvement loop (nightly ACA Job)
- [ ] E2B sandbox for diagnostic execution
- [ ] Adversarial eval set (ambiguous, multi-cause, false-positive scenarios)
- [ ] Azure Service Bus for webhook decoupling
- [ ] Blue/green deploy with eval gates in GHA

---

## Progress Log

| Date | Tasks Done | Notes |
|---|---|---|
| 2026-05-10 | 0.1 | pyproject.toml, .python-version, src/sentinel/__init__.py |
| 2026-05-10 | 0.2 | Full directory structure — 60+ stub files across all packages |
| 2026-05-10 | 0.3 | config.py with pydantic-settings, .env.example, 5 tests |
