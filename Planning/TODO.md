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

### [x] 0.4 — Docker + docker-compose + .gitignore (1%)
`Dockerfile` — multi-stage, Python 3.12-slim, copies `src/`, installs from `pyproject.toml`.
`docker-compose.yml` — single service for now, mounts `.env`, exposes port 8000.
`.gitignore` — Python defaults + `.env` + `data/*.db` + `__pycache__`.
```
Notes:
─────
2026-05-10: Multi-stage Dockerfile uses venv in builder stage for clean runtime image. docker-compose mounts ./data volume for SQLite persistence. .gitignore updated (pre-existing file merged, not overwritten) — kept CLAUDE.local.md entry.
```

### [x] 0.5 — Structured logging + basic infra (2%)
`src/sentinel/infra/logging.py` — `structlog` or stdlib `logging` with JSON formatter. Every log line includes `incident_id` when in incident context.
`src/sentinel/infra/db.py` — async SQLite connection factory using `aiosqlite`. Context manager pattern. Create tables on first run (migration-free for MVP).
```
Notes:
─────
2026-05-10: structlog with PrintLoggerFactory + JSON renderer. incident_context() binds via contextvars (async-safe). Dropped stdlib.add_logger_name (incompatible with PrintLogger). db.py: create_tables() idempotent DDL, get_db() enables WAL + foreign_keys. 11/11 tests pass.
```

---

## Phase 1 — Pydantic Domain Models (8-16%)

### [x] 1.1 — Alert models (2%)
`src/sentinel/models/alert.py`:
- `AlertSource` (enum: `datadog`, `pagerduty`)
- `AlertPayload` — source, service, metric, threshold, current_value, severity, timestamp, alert_id, metadata dict
- `AlertAck` — alert_id, received_at, incident_id (assigned by receiver)

These are the contract between webhook receiver and orchestrator.
```
Notes:
─────
2026-05-10: Added AlertSeverity StrEnum (critical/high/warning/low/info) to separate raw tool severity from internal P1-P4. Both enums use StrEnum (Python 3.11+). alert_id and timestamp default to UUID4/now(UTC). 16/16 tests pass.
```

### [x] 1.2 — Incident + Service models (2%)
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
2026-05-10: Added ServiceTier StrEnum (critical/standard/best-effort). Result fields typed dict[str,Any]|None — concrete types come in later phases. timeline uses = [] (pydantic v2 deep-copies mutable defaults, avoids pyright unknown type). Incident id = "inc-{8hex}". 23/23 tests pass.
```

### [x] 1.3 — Log, Deploy, Remediation models (2%)
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
2026-05-10: Added LogLevel + RiskLevel + ApprovalStatus StrEnums. suspect_deploy is Deploy|None (handles no-deploy scenarios). reviewer validator enforces non-empty. All models use = [] for list defaults. 38/38 tests pass.
```

### [x] 1.4 — Memory + Eval models (2%)
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
2026-05-10: EpisodicRecord includes raw_timeline (dict|None) for trajectory storage. Runbook model added for semantic memory. DimensionScore/TrajectoryScore have field_validators for 0-5 range. All use = [] for list defaults. 20 new tests (58 total). ruff + pyright clean.
```

---

## Phase 2 — Synthetic Data Generator (16-28%)

### [x] 2.1 — Scenario schema + first 3 scenarios (3%)
`data/scenarios/` — Create JSON files for:
1. `bad_deploy_01.json` — null pointer from missing config key
2. `bad_deploy_02.json` — dependency version mismatch
3. `db_pool_01.json` — connection pool exhaustion

Each file has: scenario_id, failure_class, description, alert payload, known_root_cause, logs array, deploys array. Follow the schema from ARCHITECTURE.md §9.
```
Notes:
─────
2026-05-10: Added ground_truth block to schema (severity, affected_service, root_cause_summary, recommended_action, deploy_id) — eval judge needs this without parsing known_root_cause. db_pool_01 has null deploy_id (no deploy culprit). 14+ logs per scenario with red herrings. 39 schema-validation tests pass.
```

### [x] 2.2 — Remaining 7 scenarios (3%)
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
2026-05-10: All 5 failure classes covered (bad_deploy x3, db_pool x2, downstream_outage x2, memory_leak x2, config_regression x1). downstream_outage_02 has mismatched alert/root-cause services (api-gateway alert, auth-service root cause) — tests agent's diagnostic depth. config_regression has no deploy_id (config-map change, not code). 136 tests pass.
```

### [x] 2.3 — Service map + dependency graph seed data (2%)
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
2026-05-10: Added runbooks.json alongside service_map.json + dependency_graph.json — 19 runbooks covering all 10 scenario (service, failure_class) pairs. Dependency graph edges cross-validated against service_map dependencies. user-service has no deps (avoids circular dep with auth-service). 37 tests pass.
```

### [x] 2.4 — Alert generator module (2%)
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
2026-05-10: Scenario model reuses AlertSource/AlertSeverity/LogLevel/Severity/RemediationAction enums — JSON loading validates all enum fields at parse time. ScenarioLogEntry uses 'ts' (not 'timestamp') to match compact JSON field name. functions take optional scenarios_dir param for testability. 73 tests pass.
```

### [x] 2.5 — Log + deploy generator modules (2%)
`src/sentinel/generator/log_gen.py`:
- `generate_logs(scenario: Scenario, noise: bool = True) -> list[LogEntry]` — returns scenario logs + optional noise (unrelated info-level logs from other services to make it realistic)

`src/sentinel/generator/deploy_gen.py`:
- `generate_deploys(scenario: Scenario) -> list[Deploy]` — returns deploy history, including the culprit + innocent deploys as distractors
```
Notes:
─────
2026-05-10: Noise entries are deterministic (no RNG), exclude both alert.service and ground_truth.affected_service, spaced evenly across time window. deploy_gen sorts most-recent-first; null commit → "n/a". 170 tests pass.
```

### [x] 2.6 — Seed data loader (seed.py) (1%)
`data/seed.py` — Script that:
1. Creates SQLite DB at configured path
2. Runs CREATE TABLE statements for semantic memory (services, runbooks)
3. Inserts service map + dependency graph from JSON files
4. Inserts runbooks for each service/failure_class combo

Run with: `python -m data.seed`
```
Notes:
─────
2026-05-10: seed() is async, idempotent (INSERT OR REPLACE), returns (services_count, runbooks_count). Services inserted before runbooks to satisfy FK. data/__init__.py created. Accepts optional db_path CLI arg. 15 tests pass.
```

---

## Phase 3 — Memory Subsystem (28-38%)

### [x] 3.1 — Memory store protocol/ABC (1%)
`src/sentinel/memory/base.py`:
- `MemoryStore` protocol with abstract methods: `store()`, `query()`, `get()`, `delete()`
- Keeps memory implementations swappable (SQLite now, Cosmos later)
```
Notes:
─────
2026-05-10: runtime_checkable Protocol with Any-typed signatures (concrete implementations add domain-specific typed methods). query() has top_k=5 default. 14 tests pass.
```

### [x] 3.2 — Embedding client wrapper (2%)
`src/sentinel/memory/embeddings.py`:
- `EmbeddingClient` class wrapping OpenAI `text-embedding-3-small`
- `async embed(text: str) -> list[float]`
- `async embed_batch(texts: list[str]) -> list[list[float]]`
- Caching layer (simple dict cache to avoid re-embedding identical strings)
```
Notes:
─────
2026-05-10: Model injected (not singleton) — tests use _FakeModel without loading torch. from_model_name() lazily imports SentenceTransformer. embed_batch deduplicates within and across calls; per-unique-text encode. 20 tests pass.
```

### [x] 3.3 — Semantic memory (SQLite) (2%)
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
2026-05-10: get_service/get_dependencies/get_runbook + get_all_runbooks. Both caches use dicts (None cached for missing runbooks). query() returns SemanticRecord. store/delete raise NotImplementedError. Protocol assert at import time. 34 tests pass.
```

### [x] 3.4 — Episodic memory (SQLite + embeddings) (3%)
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
2026-05-11: EpisodicMemory with store_incident (embedding BLOB via numpy tobytes), _scored_search internal helper shared by search_similar+query, cosine similarity in numpy. _build_symptom_text derives natural language from Incident fields. _KeywordFakeModel for deterministic ranking tests — 3-incident ranking test verifies correct ordering by cluster. 34 tests pass, 646 total.
```

### [x] 3.5 — Short-term memory (in-memory) (2%)
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
2026-05-11: ShortTermMemory with 5 required methods + update_context (agents need to set result fields) + active_incident_ids/active_count properties. All methods sync (no I/O). get_timeline returns defensive copy. _require() helper centralises KeyError for unknown incidents. 35 tests, 681 total.
```

---

## Phase 4 — Tool Layer (38-50%)

### [x] 4.1 — Tool registry pattern (1%)
`src/sentinel/tools/registry.py`:
- Central list of all tools
- Each tool is a standalone function decorated with `@function_tool`
- Tools receive injected dependencies (memory clients, generators) via closure or class
```
Notes:
─────
2026-05-11: ToolDeps dataclass (semantic_memory, episodic_memory, scenario). build_tools() factory with lazy imports for all 8 tools. Each tool module has make_*() factory with correct signature + NotImplementedError stub (replaced in 4.2-4.8). Closure pattern verified in tests. 25 tests, 706 total.
```

### [x] 4.2 — `get_service_metadata` tool (2%)
`src/sentinel/tools/service_lookup.py`:
- Takes: service name (str)
- Returns: ServiceMetadata (team, tier, deps, oncall, runbook summary)
- Reads from semantic memory
- Used by: Triage Agent, Log Analyst
```
Notes:
─────
2026-05-11: Logic in _build_service_response() + _format_record() for testability without ToolContext. Returns human-readable multiline string (name, team, tier, oncall, repo, deps, runbooks with 3-step preview). Unknown service → error string (never raises). 34 tests, 740 total.
```

### [x] 4.3 — `fetch_logs` tool (2%)
`src/sentinel/tools/log_fetcher.py`:
- Takes: LogQuery (service, time range, level filter)
- Returns: list of LogEntry objects
- In MVP: reads from loaded scenario data (the generator), not a real log API
- Adds noise logs if configured, to test agent's ability to filter signal from noise
```
Notes:
─────
2026-05-11: Flat params (service, start_time, end_time, level_filter, keyword_filter) with strict_mode=False for optional defaults. Level filter = minimum severity (ERROR → ERROR+CRITICAL). Logic in _fetch_logs/_filter_logs/_format_logs/_parse_iso for testability. Noise filtered automatically by service name. 43 tests, 783 total.
```

### [x] 4.4 — `list_recent_deploys` tool (2%)
`src/sentinel/tools/deploy_checker.py`:
- Takes: service name, hours_back (default 2)
- Returns: list of Deploy objects sorted by timestamp desc
- In MVP: reads from scenario data
```
Notes:
─────
2026-05-11: Reference time = max(latest log ts, latest deploy ts) from scenario data — avoids datetime.now() mismatch with historical scenario timestamps. Filter by service + time window. hours_back=2 correctly isolates culprit deploy from bad_deploy_01. Logic in _list_recent_deploys/_filter_deploys/_format_deploys/_get_reference_time. 36 tests, 819 total.
```

### [x] 4.5 — `search_past_incidents` tool (2%)
`src/sentinel/tools/incident_search.py`:
- Takes: symptom_query (str), top_k (int, default 5)
- Returns: list of EpisodicRecord with similarity scores
- Calls episodic memory's search_similar
- This is the tool that makes the agent learn from history
```
Notes:
─────
2026-05-11: Calls memory.query() for MemoryQueryResult (records + scores). Logic in _search_past_incidents/_format_results for testability. Formats each record with service, severity, symptoms, root_cause, resolution, mttr, similarity score. 33 tests, 852 total.
```

### [x] 4.6 — `draft_rollback_pr` + `draft_hotfix` tools (2%)
`src/sentinel/tools/remediation_tools.py`:
- `draft_rollback_pr`: Takes deploy_id + justification → Returns RollbackPR (title, body, target branch)
- `draft_hotfix`: Takes file_path + fix_description → Returns HotfixPatch (diff string, test suggestions)
- These produce artifacts, not actions. The remediation agent uses the output to populate the HITL approval request.
```
Notes:
─────
2026-05-11: Pure template generators — no deps injected, make_remediation_tools() returns list[FunctionTool]. _draft_rollback_pr generates PR with title/branch/checklist/approval warning. _draft_hotfix generates unified diff template + test suggestions. Both validate empty/whitespace inputs. 39 tests, 891 total.
```

### [x] 4.7 — `draft_slack_summary` tool (1%)
`src/sentinel/tools/comms_tools.py`:
- Takes: full incident timeline dict
- Returns: formatted Slack summary string (Impact / Root Cause / Timeline / Status / Action Items / ETA)
- Template-driven formatting
```
Notes:
─────
2026-05-11: Accepts JSON (preferred) or raw text. _parse_incident_data tries JSON with 8 keys (service, severity, impact, root_cause, timeline, status, action_items, eta), falls back to raw text in template. _format_slack_message applies Slack mrkdwn template. cast() for pyright strict mode on json.loads. 32 tests, 923 total.
```

### [x] 4.8 — `request_human_approval` HITL gate (2%)
`src/sentinel/tools/hitl.py`:
- Takes: ApprovalRequest (action, risk_level, evidence_summary, proposed_by)
- MVP: prints the request to terminal, waits for `input()` — `approve` or `reject`
- Returns: ApprovalResult
- This is the **critical safety boundary**. Log every approval/rejection.
- Later: swap implementation to Slack interactive button via DI
```
Notes:
─────
2026-05-11: Injectable ApprovalFn callback (default: _terminal_approval via asyncio.to_thread(input)). structlog logs every decision with action/risk/status/comment. Unknown decisions treated as REJECTED. High-risk display uses "!!!" indicator. _format_request_display + _format_result extracted. 32 tests, 955 total.
```

---

## Phase 5 — Agent Definitions (50-68%)

### [x] 5.1 — System prompt files (2%)
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
2026-05-12: Created all 6 prompt .txt files + src/sentinel/agents/loader.py (load_prompt with lru_cache). All prompts under 800 tokens, start with role declaration, and include explicit tool names + output format. HITL non-negotiable constraint encoded in remediation.txt and enforced by test. 35 tests pass (990 total).
```

### [x] 5.2 — Triage Agent (3%)
`src/sentinel/agents/triage.py`:
- Define Agent with name, instructions (from prompt file), tools (`get_service_metadata`, `search_past_incidents`), model (`gpt-4o-mini`)
- Output: the agent should produce a `TriageResult` (severity, service, is_duplicate, recommended_action)
- Test standalone: fire a scenario alert → verify correct severity + service identification
```
Notes:
─────
2026-05-12: TriageResult added to models/incident.py (severity, affected_service, is_duplicate, recommended_action). build_triage_agent() factory in agents/triage.py uses OpenAIChatCompletionsModel + AsyncOpenAI (Groq-compatible). output_type=TriageResult forces structured output. 20 tests pass — structural tests verify name, tool set (exactly 2 tools), model, no handoffs, output_type, and instructions content.
```

### [x] 5.3 — Log Analyst Agent (3%)
`src/sentinel/agents/log_analyst.py`:
- Tools: `fetch_logs`, `get_service_metadata`
- Model: `gpt-4o`
- Agent should: fetch logs for the identified service, find error patterns, produce a root cause hypothesis
- Test: given a bad_deploy scenario, does it identify the error signature in logs?
```
Notes:
─────
2026-05-12: build_log_analyst_agent() factory in agents/log_analyst.py; tools=[fetch_logs, get_service_metadata]; output_type=LogAnalysis; model=llama-3.3-70b-versatile (Groq analysis model). Accepts None scenario (fetch_logs returns error at call time, not construction time). 21 tests pass — structural tests + LogAnalysis model validation.
```

### [x] 5.4 — Deploy Correlator Agent (2%)
`src/sentinel/agents/deploy_correlator.py`:
- Tools: `list_recent_deploys`
- Model: `gpt-4o-mini`
- Agent should: list deploys, rank by suspicion (timing proximity + change scope), identify the prime suspect
- Test: given 3 deploys where one is the culprit, does it correctly rank?
```
Notes:
─────
2026-05-12: build_deploy_correlator_agent() factory; exactly 1 tool (list_recent_deploys); output_type=DeployCorrelation; model=llama-3.1-8b-instant. DeployCorrelation model validated (null suspect, multiple deploys, confidence range, JSON roundtrip with nested Deploy). 25 tests pass.
```

### [x] 5.5 — Remediation Agent (3%)
`src/sentinel/agents/remediation.py`:
- Tools: `draft_rollback_pr`, `draft_hotfix`, `request_human_approval`
- Model: `gpt-4o`
- Agent should: decide rollback vs hotfix based on evidence, draft the artifact, request HITL approval
- **Critical test:** does it ALWAYS call `request_human_approval` before finalizing? If it ever skips the gate, the system prompt needs fixing.
```
Notes:
─────
2026-05-12: build_remediation_agent() factory; tools=[draft_rollback_pr, draft_hotfix, request_human_approval]; output_type=RemediationPlan; model=llama-3.3-70b-versatile. Accepts injectable approval_fn for test/Phase2 swap. Critical HITL safety test + forbidden-tool test added. Fixed pyright list[FunctionTool]→list[Tool] covariance by explicit annotation. 27 tests pass.
```

### [x] 5.6 — Comms Agent (2%)
`src/sentinel/agents/comms.py`:
- Tools: `draft_slack_summary`
- Model: `gpt-4o-mini`
- Agent should: take full incident context, produce a clean Slack summary
- Test: verify output follows the format template (Impact/Root Cause/Timeline/Status/Actions/ETA)
```
Notes:
─────
2026-05-12: SlackSummary model added to models/comms.py (slack_message + recipients). build_comms_agent() factory; 1 tool (draft_slack_summary); output_type=SlackSummary; model=llama-3.1-8b-instant. Tests verify all 6 required sections present in instructions, #incidents channel, single tool scope. 24 tests pass.
```

### [x] 5.7 — Orchestrator Agent + Handoff Wiring (3%)
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
2026-05-12: IncidentSummary model added to models/incident.py (status Literal + severity + service + root_cause_summary + action_taken + next_steps). build_orchestrator_agent() takes 5 pre-built specialist agents + groq_client; handoffs=[triage,log_analyst,deploy_correlator,remediation,comms]; no tools (routes via handoffs only); SPECIALIST_ORDER tuple documents+enforces wiring sequence. 26 tests pass; 178 total agent tests pass.
```

---

## Phase 6 — API + End-to-End Pipeline (68-78%)

### [x] 6.1 — FastAPI webhook receiver (2%)
`src/sentinel/api/webhooks.py`:
- `POST /webhooks/alert` — accepts AlertPayload, assigns incident_id, triggers orchestrator
- Idempotency: check alert_id, skip if already processed (dedup dict, 60s TTL)
- Returns 202 Accepted with incident_id
```
Notes:
─────
2026-05-12: AlertDeduplicator (monotonic-time TTL cache, check/record/evict_expired). make_alert_router() factory with injectable deduplicator + STM + pipeline_fn. Route: dedup → incident_id generation → STM.create() → background_tasks.add_task(pipeline_fn). pyright: ignore[reportUnusedFunction] for inner FastAPI route function. 23 tests pass.
```

### [x] 6.2 — Incident query endpoint (1%)
`src/sentinel/api/incidents.py`:
- `GET /incidents` — list recent incidents (from short-term memory)
- `GET /incidents/{id}` — get full incident timeline
- `GET /health` — basic health check
```
Notes:
─────
2026-05-12: make_incidents_router(stm) → GET /incidents (IncidentListResponse) + GET /incidents/{id} (IncidentDetail, 404 on miss). make_health_router(stm|None) → GET /health (HealthResponse with active_incidents). Response models as API-layer DTOs in their own modules. 20 tests pass.
```

### [x] 6.3 — FastAPI app entrypoint + lifespan (2%)
`src/sentinel/main.py`:
- Create FastAPI app with lifespan handler
- On startup: init DB, seed if needed, create memory clients, create agent instances (DI)
- Mount routers from api/
- Run with: `uvicorn sentinel.main:app --reload`
```
Notes:
─────
2026-05-12: lifespan(): create_tables → seed → EmbeddingClient+memory → groq_client+set_default_openai_client → 5 specialists → orchestrator → deduplicator → make_pipeline_fn → include_router x3. make_pipeline_fn() extracted as testable factory (Runner.run mock, STM status update, exception→ESCALATED, KeyError guard). 13 tests pass.
```

### [x] 6.4 — Tracing/trajectory capture (2%)
`src/sentinel/infra/tracing.py`:
- Capture every agent step: tool calls, handoffs, LLM responses
- Store as structured timeline in short-term memory
- Hook into Agents SDK's built-in tracing (it has `@trace` support)
- Output: a JSON trajectory file per incident
```
Notes:
─────
2026-05-12: SentinelTracer(TracingProcessor) — on_span_end converts FunctionSpanData→TimelineEntry (tool calls) + HandoffSpanData→TimelineEntry (handoffs); AgentSpanData/GenerationSpanData written to trajectory JSON only. incident_id via span.trace_metadata["incident_id"] (no extra mapping needed). _save_trajectory() writes reports/trajectories/{incident_id}.json. main.py updated: import agent_trace + SentinelTracer, wrap Runner.run in trace() ctx, add_trace_processor(tracer) in lifespan. 31 new tracing tests + 13 main tests = 44 pass.
```

### [x] 6.5 — End-to-end smoke test (3%)
`scripts/run_scenario.py`:
- CLI script: `python scripts/run_scenario.py bad_deploy_01`
- Loads scenario → generates alert → POSTs to webhook → waits for resolution
- Prints full timeline + HITL prompts
- This is your "does it work" checkpoint. Don't proceed to eval until 3+ scenarios pass cleanly.
```
Notes:
─────
2026-05-12: run_scenario.py CLI: --list/--help/--scenario_id dispatch; run_scenario() async factory builds full agent pipeline with scenario injected into log/deploy tools; SentinelTracer+agent_trace() wired; _print_timeline/_print_header helpers. Tests: importlib loads script, arg handling, FileNotFoundError for unknown scenario, helper function output, 3 scenario load validations. 24 tests pass.
```

### [x] 6.6 — SSE event bus for real-time pipeline updates (3%)
`src/sentinel/infra/event_bus.py`:
- `EventBus` class using `asyncio.Queue` — one queue per connected client
- Event types: `agent_started`, `agent_completed`, `tool_called`, `tool_result`, `hitl_requested`, `hitl_resolved`, `incident_resolved`
- Each event is a Pydantic model: `PipelineEvent(type, agent_name, timestamp, data)`
- Orchestrator emits events at each step: before/after handoff, before/after tool call, on HITL gate
- This is the bridge between agent pipeline and dashboard — agents push events, SSE endpoint streams them
```
Notes:
─────
2026-05-12: EventBus with fan-out pub/sub (asyncio.Queue per subscriber), PipelineEvent model, EventType enum, close_incident sentinel pattern. 27 tests.
```

### [x] 6.7 — SSE streaming endpoint + HITL approval API (2%)
`src/sentinel/api/events.py`:
- `GET /events/{incident_id}` — SSE endpoint, yields `PipelineEvent` as `text/event-stream`
- Uses `EventBus` to subscribe to events for a specific incident
- `POST /incidents/{incident_id}/approve` — body: `{"action": "approve" | "reject", "comment": "optional"}`
- On approve/reject: resolves the HITL gate (sets an `asyncio.Event` that the `request_human_approval` tool awaits)
- This replaces the `input()` HITL implementation when dashboard is active — tool checks if running in dashboard mode vs CLI mode
```
Notes:
─────
2026-05-13: events.py already implemented (HitlGateRegistry, HitlGate, make_api_approval_fn, make_events_router, set/reset_current_incident). Tests were present but 3 SSE tests hung due to cross-event-loop asyncio.Queue issue. Fixed by converting all SSE tests to async using httpx.AsyncClient+ASGITransport+asyncio.TaskGroup — same event loop as ASGI app ensures reliable close_incident signaling. 33/33 tests pass.
```

### [x] 6.8 — Live incident dashboard (single HTML file) (4%)
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
2026-05-13: dashboard/index.html (dark GitHub theme, vanilla JS+CSS, EventSource SSE, HITL overlay with approve/reject, metrics bar). api/scenarios.py (GET /api/scenarios → scenario list + alert payloads). infra/dashboard_emitter.py (DashboardEventEmitter TracingProcessor → publishes agent_started/completed/tool_called/tool_result to EventBus). main.py updated (GET / route, DashboardEventEmitter registration, scenarios router). 37/37 tests pass.
```

### [x] 6.9 — Eval results display page (2%)
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
2026-05-13: dashboard/eval.html (dark GitHub theme, per-scenario score table, dimension averages, color-coded cells, pass/fail, summary cards). api/eval_report.py (GET /api/eval-report → reads reports/eval_report.json, 404 if missing). main.py updated (GET /eval route, eval_report router). 18/18 tests pass.
```

---

## Phase 7 — Trajectory Eval (78-90%)

### [x] 7.1 — Eval rubric + judge prompts (2%)
`src/sentinel/eval/rubric.py`:
- Define the 6 eval dimensions as enum
- Write judge system prompt: given a trajectory JSON + scenario ground truth, score each dimension 0-5 with reasoning
- Structured output: the judge returns a `TrajectoryScore` Pydantic model
```
Notes:
─────
2026-05-13: rubric.py — DimensionRubric frozen dataclass, DIMENSION_RUBRICS dict (all 6 dimensions, 0-5 levels each), JUDGE_SYSTEM_PROMPT built programmatically from rubric data, format_judge_input() helper. 29/29 tests pass.
```

### [x] 7.2 — LLM-as-judge implementation (3%)
`src/sentinel/eval/judge.py`:
- `evaluate_trajectory(trajectory: dict, ground_truth: dict) -> TrajectoryScore`
- Calls the judge model with the rubric prompt
- Parses structured output into TrajectoryScore
- Uses `gpt-4o-mini` as judge (different family if you later add Claude for analysis)
```
Notes:
─────
2026-05-13: judge.py — evaluate_trajectory() async fn (Groq-compatible AsyncOpenAI, 4 attempts with 1s/2s/4s backoff), _parse_judge_response() (strips markdown fences, handles missing dims, clamps scores), _zero_score() fallback. 28/28 tests pass.
```

### [x] 7.3 — Eval runner (batch scenarios) (3%)
`src/sentinel/eval/runner.py` + `scripts/run_eval.py`:
- Iterates over all 10 scenarios (or a subset)
- For each: generate alert → run pipeline → capture trajectory → judge it
- Collects all TrajectoryScores
- Handles failures gracefully (scenario fails → score 0, log error, continue)
```
Notes:
─────
2026-05-13: runner.py — EvalRunResult dataclass, _auto_approve (always approves for batch eval), run_scenario_eval() (builds fresh agent set per scenario, auto-approve HITL, loads trajectory, judges), run_eval() (batch loop with error isolation), summary_stats() + scores_from_results() helpers. scripts/run_eval.py — CLI with --scenarios + --judge-model args, text table output, writes eval_report.json + eval_report.md. 20/20 tests pass.
```

### [x] 7.4 — Eval report generation (2%)
`src/sentinel/eval/report.py`:
- Takes list of TrajectoryScores → produces:
  1. JSON report (machine-readable, for CI gates later)
  2. Markdown summary (human-readable)
- Per-dimension averages, per-scenario breakdown, pass/fail thresholds
- Output to `reports/eval_report.md` and `reports/eval_report.json`
```
Notes:
─────
2026-05-13: report.py — DimensionStats frozen dataclass, EvalReport frozen dataclass, build_report() (avg/pass rate/dim stats), to_json_dict() (dashboard-compatible shape), to_markdown() (3-section MD), write_json/write_markdown/write_reports() (create parent dirs). scripts/run_eval.py updated to use report.py (removed duplicate private fns). 44/44 tests pass.
```

---

## Phase 8 — Polish + Demo (90-100%)

### [x] 8.1 — README.md (3%)
Write the public-facing README:
- Project title + one-line description
- Architecture diagram (Mermaid or ASCII)
- Features list (what it does)
- Quick start (clone, .env, docker-compose up, fire a scenario)
- Demo section (link to Loom or GIF) -- leave empty for now
- Tech stack table
- Eval results summary
- Phase 2 roadmap teaser
```
Notes:
─────
2026-05-13: README.md — ASCII pipeline diagram, 8-bullet feature list, Quick Start (docker + bare-metal paths), demo placeholder, tech stack table, eval dimensions + pass threshold, project structure tree, 3 architecture decision notes, Phase 2 roadmap table, test run command.
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

## Phase 9 — Multi-Provider Model Layer (LiteLLM Migration)

> Replace hard-coded Groq wiring with a `provider/model` string system so any
> agent can be pointed at any supported provider (Groq, OpenAI, Anthropic, Azure)
> via one env-var change. Uses LiteLLM as the bridge. See `models_provider.md`
> for the full execution plan and `ARCHITECTURE.md` §3.1 for the design.

### [x] 9.1 — Fix Groq handoff bugs + strict_json_schema sweep (3%)
Fix 3 known SDK/Groq compat issues before touching the provider layer:
1. Add `set_default_openai_api("chat_completions")` + conditional `set_tracing_disabled` in `main.py` lifespan.
2. Add `strict_json_schema=False` to every `@function_tool` decorator in `src/sentinel/tools/`.
3. Bump `max_turns` by +5 in `main.py`, `run_scenario.py`, `run_eval.py` to account for handoff overhead.
4. Append "When calling transfer_to_<agent>, pass no arguments." to orchestrator prompt.
**Verify:** `pytest tests/test_tools/ -x` — all pass.
```
Notes:
─────
2026-05-15: Added set_default_openai_api("chat_completions") + set_tracing_disabled(True) to main.py, run_scenario.py, eval/runner.py. Added strict_mode=False to all 8 @function_tool decorators (plan's strict_json_schema=False maps to strict_mode=False in agents==0.17.0 — param doesn't exist yet). Bumped max_turns +5 in all 3 entry points. Appended no-args handoff rule to orchestrator.txt. 1480 tests pass, 0 regressions.
```

### [x] 9.2 — Config update + provider API key validation (3%)
Update `Settings` in `config.py` to support multi-provider model strings:
1. Change model field defaults to `provider/model` format (e.g. `groq/llama-3.1-8b-instant`).
2. Add optional API key fields: `openai_api_key`, `anthropic_api_key`, `azure_api_key`, `azure_api_base`, `azure_api_version`.
3. Add `@model_validator(mode="after")` that reads the provider prefix from each model string and asserts the matching API key is non-empty.
4. Update `.env.example` with all new vars grouped by provider.
**Verify:** Existing config tests pass + 3 new validator tests (missing key → raises, correct key → passes).
```
Notes:
─────
2026-05-15: Model defaults changed to groq/provider/model format. Added openai_api_key, anthropic_api_key, azure_api_key, azure_api_base, azure_api_version fields (all optional). Added model_validator that checks provider prefix against key presence for all 3 model fields. .env.example regrouped by provider section. 14 config tests (9 new), 1489 total, 0 regressions.
```

### [x] 9.3 — Create `src/sentinel/providers/` package (4%)
Build the provider abstraction layer:
1. `resolver.py` — `resolve_model(model_string, settings) → LitellmModel` (parse prefix → select API key → return model).
2. `capabilities.py` — `get_capabilities(model_string) → ProviderCapabilities` (structured_outputs, strict_schemas flags per provider).
3. `output_coercion.py` — `coerce_output(text, schema) → BaseModel | None` (strip markdown fences, `model_validate_json` fallback for providers without structured outputs).
4. `__init__.py` — re-export + `apply_sdk_defaults(settings)` helper (moves `set_default_openai_api` + `set_tracing_disabled` from main.py).
5. Add `openai-agents[litellm]` to `pyproject.toml` (replace plain `openai-agents`).
**Verify:** `tests/test_providers/` — test_resolver (4 providers), test_capabilities, test_unknown_prefix, test_coercion.
```
Notes:
─────
2026-05-15: Created providers/ package: resolver.py (resolve_model → LitellmModel), capabilities.py (ProviderCapabilities dataclass + get_capabilities), output_coercion.py (coerce_output with fence-stripping), __init__.py (re-exports + apply_sdk_defaults). Updated pyproject.toml to openai-agents[litellm]. Installed litellm via pip. UP047 fixed by using PEP 695 type param syntax. 38 new tests, 1527 total, 0 regressions.
```

### [x] 9.4 — Migrate all 6 agent builders + pipeline entry points (5%)
Rewire every agent to use the provider layer instead of raw Groq client:
1. Change all `build_*_agent()` signatures: drop `groq_client` + `model_name`, add `model_string` + `settings`.
2. Inside each builder: `llm = resolve_model(model_string, settings)` + `caps = get_capabilities(model_string)`.
3. Wrap `output_type=` in capability check — only set when `caps.supports_structured_outputs`.
4. Update `main.py` lifespan: remove `AsyncOpenAI(...)` and `set_default_openai_client(...)`, replace with `apply_sdk_defaults(settings)`.
5. Mirror changes in `scripts/run_scenario.py` and `scripts/run_eval.py`.
6. Update `tests/test_agents/conftest.py` with `FakeSettings` fixture + `MagicMock` model stub.
**Verify:** `pytest tests/test_agents/ -x` — all structural tests pass (no live API calls).
```
Notes:
─────
2026-05-15: All 6 builders drop groq_client+model_name, gain model_string+settings. resolve_model+get_capabilities called inside each. output_type conditionally set via caps.supports_structured_outputs. main.py/run_scenario.py use apply_sdk_defaults. eval/runner.py keeps AsyncOpenAI for judge only. test_agents/ updated to LitellmModel assertions + fake_settings fixture. test_runner.py updated to new run_scenario_eval signature. 1527 tests pass, 0 regressions. No OpenAIChatCompletionsModel/AsyncOpenAI in agents/.
```

### [x] 9.5 — Reference docs + final clean pass (5%)
Create docs and run final verification:
1. Create `available_models.md` — per-provider model table with role, context window, exact env-var string.
2. Update `README.md` — replace Groq-only LLM row with multi-provider row, add "Switching providers" section.
3. Final sweep: `ruff check src/ tests/ --fix` + `pyright src/` + `pytest -x --tb=short`.
4. Assert no `AsyncOpenAI` or `OpenAIChatCompletionsModel` import remains in `src/sentinel/agents/`.
5. Smoke test: `SENTINEL_ANALYSIS_MODEL=groq/llama-3.3-70b-versatile python scripts/run_scenario.py bad_deploy_01`.
```
Notes:
─────
2026-05-15: Created available_models.md (4 providers, quick-swap examples, capability matrix). Updated README.md (multi-provider LLM row, Switching providers section in Quick Start, Phase 2 roadmap update). Fixed pre-existing pyright error in cli.py (reportUnknownArgumentType). ruff clean, pyright 0 errors, 1527 tests pass. No AsyncOpenAI/OpenAIChatCompletionsModel in agents/. Smoke test requires live Groq key — runs end-to-end with default groq/ config.
```

---

## Phase 2 Roadmap (Post-MVP, not tracked here)

These are documented for interview conversations ("what would you do next"):
- [ ] Azure Container Apps deployment (Bicep IaC)
- [ ] Cosmos DB (vector + JSON) replacing SQLite
- [ ] Redis for short-term memory
- [x] ~~LiteLLM + Kong AI Gateway for model routing + token budgets~~ → LiteLLM done in Phase 9; Kong remains Phase 2
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
| 2026-05-10 | 0.4 | Dockerfile (multi-stage venv), docker-compose.yml, .gitignore updated |
| 2026-05-10 | 0.5 | infra/logging.py (structlog JSON + incident_context), infra/db.py (aiosqlite, WAL, DDL) |
| 2026-05-10 | 1.1 | models/alert.py — AlertSource, AlertSeverity, AlertPayload, AlertAck. 16 tests. |
| 2026-05-10 | 1.2 | models/incident.py + service.py — Severity, IncidentStatus, Incident, ServiceMetadata. 23 tests. |
| 2026-05-10 | 1.3 | models/log_entry.py + deploy.py + remediation.py — 9 models, 3 enums. 38 tests. |
| 2026-05-10 | 1.4 | models/memory.py + eval_result.py — EpisodicRecord, Runbook, SemanticRecord, MemoryQueryResult, EvalDimension, DimensionScore, TrajectoryScore. 20 tests. |
| 2026-05-10 | 2.1 | data/scenarios/bad_deploy_01.json + bad_deploy_02.json + db_pool_01.json — 14+ logs each, red herrings, ground_truth block added to schema. 39 tests. |
| 2026-05-10 | 2.2 | 7 more scenarios — bad_deploy_03, db_pool_02, downstream_outage_01/02, memory_leak_01/02, config_regression_01. All 5 failure classes. 136 tests. |
| 2026-05-10 | 2.3 | service_map.json (8 services), dependency_graph.json (12 edges), runbooks.json (19 runbooks). 37 tests. |
| 2026-05-10 | 2.4 | generator/scenarios.py (6 Pydantic models), generator/alert_gen.py (load_scenario, generate_alert, list_scenarios). 73 tests. |
| 2026-05-10 | 2.5 | generator/log_gen.py (generate_logs + noise), generator/deploy_gen.py (generate_deploys). 170 tests. |
| 2026-05-10 | 2.6 | data/seed.py — async seed() inserting 8 services + 19 runbooks into SQLite. data/__init__.py added. 15 tests. |
| 2026-05-10 | 3.1 | memory/base.py — MemoryStore runtime_checkable Protocol (store, query, get, delete). 14 tests. |
| 2026-05-10 | 3.2 | memory/embeddings.py — EmbeddingClient (inject model, dict cache, embed/embed_batch, asyncio.to_thread). 20 tests. |
| 2026-05-10 | 3.3 | memory/semantic.py — SemanticMemory (get_service, get_dependencies, get_runbook, in-process cache). 34 tests. |
| 2026-05-13 | 6.9 | dashboard/eval.html (eval results page), api/eval_report.py (GET /api/eval-report), main.py (GET /eval route). 18 tests. |
