# Sentinel — Architecture Reference

> Autonomous DevOps Incident Response Agent
> This document is the **single source of truth** for implementation decisions. Code-facing only — no fluff.

---

## 1. What This System Does (60-Second Version)

```
Alert fires → Orchestrator receives it → Triage classifies severity + service
→ Log Analyst fetches & analyzes logs → Deploy Correlator finds suspicious deploys
→ Remediation Agent drafts rollback/hotfix → Comms Agent drafts Slack summary
→ HITL gate: human approves → action executes → memory stores the incident
→ Eval scores the full trajectory
```

The agent makes 6-10 tool calls per incident. Every destructive action is gated behind human approval. Memory of past incidents improves future triage.

---

## 2. MVP vs Full Scope

| Concern | MVP (what we build now) | Full (Phase 2) |
|---|---|---|
| Alert source | Local FastAPI endpoint, synthetic JSON payloads | ACA Job on cron, Service Bus |
| Orchestration | OpenAI Agents SDK, local process | ACA container, Service Bus subscriber |
| Sub-agents | 5 specialists via SDK Handoffs | Same, but with timeout/retry |
| Memory | SQLite (episodic + semantic), in-memory (short-term) | Cosmos DB vector + JSON, Redis |
| HITL | CLI prompt (`input()`) or simple web UI | Slack interactive buttons, GitHub PR review |
| Eval | LLM-as-judge, local JSON reports | LangFuse traces, dashboard |
| Model routing | OpenAI API directly (single provider OK for MVP) | LiteLLM + Kong AI Gateway |
| Infra | `docker-compose` local | Bicep → ACA, Azure Front Door |
| CI/CD | Basic GHA lint + test | GHA with eval gates, blue/green |
| Scenarios | 10 scenarios, 4 failure classes | 40 scenarios, 8 classes |
| Self-improvement | Skip for MVP | Nightly ACA Job, auto-PR |

---

## 3. Tech Stack (MVP)

```
Runtime:          Python 3.12+
Agent framework:  openai-agents (OpenAI Agents SDK)
LLM:              LLM: Groq API (OpenAI-compatible) — llama-3.3-70b-versatile for orchestration + analysis, llama-3.1-8b-instant for triage + eval judge. Gemini 2.5 Flash as backup via google-genai SDK.
Tool schemas:     Pydantic v2
Memory store:     SQLite (via aiosqlite) — episodic + semantic tables
Short-term mem:   Python dict (per-incident, in-process)
Embeddings:       Embeddings: sentence-transformers all-MiniLM-L6-v2 (local, zero cost) — 384 dims. Update SQLite embedding BLOB schema accordingly.
API layer:        FastAPI + uvicorn
HTTP client:      httpx (async)
Testing:          pytest + pytest-asyncio
Containerization: Docker + docker-compose
Linting:          ruff
Type checking:    pyright or mypy
```

---

## 4. Directory Structure

```
sentinel/
├── ARCHITECTURE.md          ← this file
├── TODO.md                  ← task tracker
├── README.md                ← public-facing (write last)
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .github/
│   └── workflows/
│       └── ci.yml
├── src/
│   └── sentinel/
│       ├── __init__.py
│       ├── config.py                  # env-var driven settings (pydantic-settings)
│       ├── main.py                    # FastAPI app entrypoint
│       │
│       ├── api/                       # HTTP layer
│       │   ├── __init__.py
│       │   ├── webhooks.py            # POST /webhooks/alert — receives alerts
│       │   ├── incidents.py           # GET /incidents — list/query incidents
│       │   └── health.py              # GET /health
│       │
│       ├── models/                    # Pydantic domain models (no DB coupling)
│       │   ├── __init__.py
│       │   ├── alert.py               # Alert, AlertPayload, AlertSource
│       │   ├── incident.py            # Incident, IncidentTimeline, Severity
│       │   ├── service.py             # ServiceMetadata, DependencyGraph
│       │   ├── deploy.py              # Deploy, DeployCorrelation
│       │   ├── log_entry.py           # LogEntry, LogQuery, LogAnalysis
│       │   ├── remediation.py         # RemediationPlan, RollbackPR, HotfixPatch
│       │   ├── eval_result.py         # TrajectoryScore, EvalDimension
│       │   └── memory.py             # EpisodicRecord, SemanticRecord
│       │
│       ├── agents/                    # Agent definitions (system prompts + tool bindings)
│       │   ├── __init__.py
│       │   ├── orchestrator.py        # Top-level ReAct agent, handoff router
│       │   ├── triage.py              # Severity classification, service identification
│       │   ├── log_analyst.py         # Log fetching + pattern analysis
│       │   ├── deploy_correlator.py   # Recent deploy lookup + suspicion ranking
│       │   ├── remediation.py         # Rollback/hotfix drafting
│       │   └── comms.py               # Slack summary drafting
│       │
│       ├── tools/                     # Tool implementations (what agents call)
│       │   ├── __init__.py
│       │   ├── registry.py            # Central tool registration
│       │   ├── service_lookup.py      # get_service_metadata
│       │   ├── log_fetcher.py         # fetch_logs
│       │   ├── deploy_checker.py      # list_recent_deploys
│       │   ├── incident_search.py     # search_past_incidents (episodic memory)
│       │   ├── remediation_tools.py   # draft_rollback_pr, draft_hotfix
│       │   ├── comms_tools.py         # draft_slack_summary
│       │   └── hitl.py                # request_human_approval (the gate)
│       │
│       ├── memory/                    # Memory subsystem
│       │   ├── __init__.py
│       │   ├── base.py                # MemoryStore protocol/ABC
│       │   ├── short_term.py          # In-memory dict, per-incident
│       │   ├── episodic.py            # SQLite — past incidents, vector-indexed
│       │   ├── semantic.py            # SQLite — service map, deps, runbooks
│       │   └── embeddings.py          # OpenAI embedding client wrapper
│       │
│       ├── eval/                      # Trajectory evaluation
│       │   ├── __init__.py
│       │   ├── judge.py               # LLM-as-judge with structured rubric
│       │   ├── rubric.py              # Scoring dimensions + prompts
│       │   ├── runner.py              # Run eval across scenario set
│       │   └── report.py              # Generate JSON/markdown eval reports
│       │
│       ├── generator/                 # Synthetic alert + log + deploy data
│       │   ├── __init__.py
│       │   ├── scenarios.py           # Scenario definitions (known root causes)
│       │   ├── alert_gen.py           # Generate realistic alert payloads
│       │   ├── log_gen.py             # Generate correlated log entries
│       │   └── deploy_gen.py          # Generate deploy history
│       │
│       └── infra/                     # Cross-cutting concerns
│           ├── __init__.py
│           ├── db.py                  # SQLite connection management
│           ├── logging.py             # Structured logging setup
│           └── tracing.py             # Agent trace capture (for eval)
│
├── data/
│   ├── scenarios/                     # JSON scenario files
│   │   ├── bad_deploy_01.json
│   │   ├── db_pool_exhaustion_01.json
│   │   └── ...
│   ├── services/                      # Seed data for semantic memory
│   │   ├── service_map.json           # Service ownership + metadata
│   │   └── dependency_graph.json      # Service-to-service deps
│   └── seed.py                        # Load seed data into SQLite
│
├── tests/
│   ├── conftest.py
│   ├── test_agents/
│   ├── test_tools/
│   ├── test_memory/
│   ├── test_eval/
│   └── test_api/
│
└── scripts/
    ├── run_scenario.py               # Fire a single scenario end-to-end
    ├── run_eval.py                    # Run full eval suite
    └── demo.py                       # Interactive demo runner
```

---

## 5. Agent Definitions

### 5.1 Orchestrator

**Role:** Top-level coordinator. Receives an alert, decides which specialist to invoke next, maintains incident timeline, enforces tool-call cap (15 max).

**SDK setup:**
```python
from agents import Agent, Runner, handoff

orchestrator = Agent(
    name="incident_orchestrator",
    instructions=ORCHESTRATOR_SYSTEM_PROMPT,  # loaded from file
    handoffs=[triage_agent, log_analyst, deploy_correlator, remediation_agent, comms_agent],
    model="gpt-4o",
)
```

**Key behaviors:**
- On new alert → handoff to Triage
- After triage → handoff to Log Analyst
- After log analysis → handoff to Deploy Correlator
- After deploy correlation → handoff to Remediation (if root cause found)
- After remediation draft → handoff to Comms
- After comms → return final incident summary
- At any point if HITL approval needed → call `request_human_approval` tool

**Handoff pattern:** The orchestrator uses `handoff()` to delegate to specialists. Each specialist returns control to the orchestrator when done. The orchestrator decides the next step.

### 5.2 Triage Agent

**Input:** Raw alert payload
**Output:** `TriageResult(severity, affected_service, is_duplicate, recommended_action)`
**Tools:** `get_service_metadata`, `search_past_incidents`
**Model:** `gpt-4o-mini` (fast, structured output)
**System prompt focus:** Classify severity (P1-P4), identify the affected service from alert metadata, check for duplicate active incidents, decide if escalation is needed.

### 5.3 Log Analyst

**Input:** Service name + time window from triage
**Output:** `LogAnalysis(error_patterns, anomaly_summary, key_log_lines, hypothesis)`
**Tools:** `fetch_logs`
**Model:** `gpt-4o` (long context for log dumps, nuanced pattern recognition)
**System prompt focus:** Fetch logs for the identified service in the relevant time window. Identify error patterns, stack traces, rate changes. Summarize findings into a hypothesis.

### 5.4 Deploy Correlator

**Input:** Service name + incident timestamp
**Output:** `DeployCorrelation(recent_deploys, suspect_deploy, confidence, evidence)`
**Tools:** `list_recent_deploys`
**Model:** `gpt-4o-mini`
**System prompt focus:** List deploys to the affected service in the last 2 hours. Rank by suspicion (timing, author, change size, related files). Identify the most likely culprit.

### 5.5 Remediation Agent

**Input:** Root cause hypothesis + suspect deploy + log evidence
**Output:** `RemediationPlan(action_type, rollback_pr | hotfix_diff, risk_assessment)`
**Tools:** `draft_rollback_pr`, `draft_hotfix`, `request_human_approval`
**Model:** `gpt-4o` (reasoning-heavy)
**System prompt focus:** Based on evidence, decide between rollback and hotfix. Draft the remediation artifact. Always go through HITL gate before any action. Include risk assessment.

### 5.6 Comms Agent

**Input:** Full incident timeline + triage + analysis + remediation plan
**Output:** `SlackSummary(impact, root_cause, timeline, action_items, eta)`
**Tools:** `draft_slack_summary`
**Model:** `gpt-4o-mini`
**System prompt focus:** Draft a structured incident summary for a #incidents Slack channel. Standard format: Impact → Root Cause → Timeline → Current Status → Action Items → ETA.

---

## 6. Tool Layer Design

Every tool is a Python function decorated with `@function_tool` from the Agents SDK. Input/output typed with Pydantic.

```python
from agents import function_tool
from sentinel.models.log_entry import LogQuery, LogAnalysis

@function_tool
async def fetch_logs(query: LogQuery) -> LogAnalysis:
    """Fetch and return logs for a service within a time window."""
    # In MVP: returns synthetic logs from generator
    # In prod: would call Datadog/CloudWatch API
    ...
```

### Tool inventory (MVP):

| Tool | Agent(s) | Input | Output | Destructive? |
|---|---|---|---|---|
| `get_service_metadata` | Triage, Log Analyst | service_name | ServiceMetadata | No |
| `fetch_logs` | Log Analyst | LogQuery (service, time_range, filters) | LogAnalysis | No |
| `list_recent_deploys` | Deploy Correlator | service_name, hours_back | list[Deploy] | No |
| `search_past_incidents` | Triage, Orchestrator | symptom_query, top_k | list[EpisodicRecord] | No |
| `draft_rollback_pr` | Remediation | deploy_id, justification | RollbackPR | No (draft only) |
| `draft_hotfix` | Remediation | file_path, fix_description | HotfixPatch | No (draft only) |
| `draft_slack_summary` | Comms | IncidentTimeline | SlackSummary | No |
| `request_human_approval` | Remediation, Orchestrator | action_description, risk_level | ApprovalResult | **HITL GATE** |

**Key pattern:** No tool directly mutates external state. `draft_*` tools produce artifacts. `request_human_approval` is the gate. Only after approval does the orchestrator trigger the actual action (which in MVP is a print/log).

---

## 7. Memory Architecture (MVP)

### 7.1 Short-Term Memory
- **What:** Current incident's timeline — every tool call, every handoff, every result
- **Storage:** Python `dict` keyed by `incident_id`, lives in-process
- **Lifetime:** Created on alert intake, discarded after incident resolution + eval
- **Structure:**
```python
{
    "incident_id": "inc-20260510-001",
    "alert": AlertPayload,
    "timeline": [
        {"ts": "...", "agent": "triage", "action": "get_service_metadata", "result": {...}},
        {"ts": "...", "agent": "log_analyst", "action": "fetch_logs", "result": {...}},
        ...
    ],
    "triage_result": TriageResult,
    "log_analysis": LogAnalysis,
    "deploy_correlation": DeployCorrelation,
    "remediation_plan": RemediationPlan,
    "slack_summary": SlackSummary,
    "status": "resolved" | "pending_approval" | "escalated"
}
```

### 7.2 Episodic Memory (SQLite)
- **What:** Every resolved incident — symptoms, root cause, fix, MTTR
- **Indexed by:** Embedding of symptom description (for similarity search)
- **Table schema:**
```sql
CREATE TABLE episodic_incidents (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    service_name TEXT NOT NULL,
    severity TEXT NOT NULL,
    symptoms TEXT NOT NULL,           -- natural language description
    root_cause TEXT NOT NULL,
    resolution TEXT NOT NULL,
    mttr_seconds INTEGER,
    embedding BLOB,                   -- serialized float32 array
    raw_timeline JSON                 -- full incident timeline
);
```
- **Query:** On new incident, embed the current symptoms → cosine similarity against stored embeddings → return top-5 similar past incidents. The triage agent uses these to inform its classification.

### 7.3 Semantic Memory (SQLite)
- **What:** Service ownership, dependency graph, on-call schedule, runbooks
- **Loaded:** Once per incident, cached in short-term memory
- **Tables:**
```sql
CREATE TABLE services (
    name TEXT PRIMARY KEY,
    team TEXT NOT NULL,
    tier TEXT NOT NULL,               -- "critical" | "standard" | "best-effort"
    oncall_channel TEXT,
    repo_url TEXT,
    description TEXT,
    dependencies JSON                 -- ["service-a", "service-b"]
);

CREATE TABLE runbooks (
    id TEXT PRIMARY KEY,
    service_name TEXT NOT NULL,
    failure_class TEXT NOT NULL,       -- "bad_deploy", "db_pool", etc.
    steps JSON NOT NULL,              -- ordered remediation steps
    FOREIGN KEY (service_name) REFERENCES services(name)
);
```

### 7.4 Embedding Strategy
- Model: Model: all-MiniLM-L6-v2 (384 dims, local, free) — swap to text-embedding-3-small for prod
- Similarity: Cosine similarity computed in Python (no vector DB needed for MVP scale)
- Store as BLOB in SQLite, deserialize with numpy

---

## 8. HITL Gate Design

```python
from enum import Enum
from pydantic import BaseModel

class ApprovalStatus(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"

class ApprovalRequest(BaseModel):
    action: str                       # "rollback deploy abc123"
    risk_level: str                   # "high" | "medium" | "low"
    evidence_summary: str             # why the agent wants to do this
    proposed_by: str                  # which agent

class ApprovalResult(BaseModel):
    status: ApprovalStatus
    reviewer: str
    comment: str | None = None
```

**MVP implementation:** `input()` prompt in the terminal. The tool blocks until the human types `approve` or `reject`. In Phase 2, this becomes a Slack interactive button or GitHub PR review webhook.

**Critical rule:** The `request_human_approval` tool is the ONLY path to destructive actions. No agent can bypass it. This is enforced by tool design — there is no "execute rollback" tool, only "draft rollback" + "request approval."

---

## 9. Synthetic Data Generator

Each scenario is a JSON file defining:
```json
{
    "scenario_id": "bad_deploy_01",
    "failure_class": "bad_deploy",
    "description": "API gateway deploy introduces null pointer in auth middleware",
    "alert": {
        "source": "datadog",
        "service": "api-gateway",
        "metric": "error_rate",
        "threshold": 0.05,
        "current_value": 0.34,
        "severity": "critical"
    },
    "known_root_cause": {
        "type": "bad_deploy",
        "deploy_id": "deploy-abc123",
        "commit": "a1b2c3d",
        "description": "Null pointer in auth middleware due to missing config key"
    },
    "logs": [
        {"ts": "2026-05-10T03:00:01Z", "level": "ERROR", "service": "api-gateway", "message": "NullPointerException in AuthMiddleware.validate()"},
        {"ts": "2026-05-10T03:00:02Z", "level": "ERROR", "service": "api-gateway", "message": "Request failed: /api/v1/users — 500 Internal Server Error"}
    ],
    "deploys": [
        {"id": "deploy-abc123", "service": "api-gateway", "ts": "2026-05-10T02:45:00Z", "author": "dev-bot", "commit": "a1b2c3d", "files_changed": 3},
        {"id": "deploy-xyz789", "service": "api-gateway", "ts": "2026-05-09T14:00:00Z", "author": "keshav", "commit": "x7y8z9a", "files_changed": 1}
    ]
}
```

**MVP scenarios (10 total, 4 failure classes):**
1. `bad_deploy_01` — null pointer from bad config
2. `bad_deploy_02` — dependency version mismatch
3. `bad_deploy_03` — missing env var in new deploy
4. `db_pool_01` — connection pool exhaustion under load
5. `db_pool_02` — connection leak from unclosed cursors
6. `downstream_outage_01` — third-party payment API down
7. `downstream_outage_02` — internal auth service timeout cascade
8. `memory_leak_01` — gradual OOM from unbounded cache
9. `memory_leak_02` — event listener accumulation
10. `config_regression_01` — feature flag rollout breaks subset of users

---

## 10. Trajectory Eval Design

**What we eval:** The full incident arc, not individual LLM responses.

**Dimensions (0-5 each):**

| Dimension | What it measures |
|---|---|
| `triage_accuracy` | Did it identify the right service and correct severity? |
| `root_cause_correctness` | Does the hypothesis match the known root cause from the scenario? |
| `tool_efficiency` | Did it use the right tools in a logical order? No unnecessary calls? |
| `mttr` | Time from alert to remediation proposal (lower is better) |
| `remediation_safety` | Did it gate destructive actions? Was the proposed fix appropriate? |
| `comms_quality` | Is the Slack summary clear, complete, correctly structured? |

**Judge setup:**
- Model: `gpt-4o-mini` (different family if Claude was used for analysis — cross-family eval)
- Input: Full incident trajectory (timeline JSON) + scenario ground truth
- Output: `TrajectoryScore` (Pydantic model with per-dimension scores + reasoning)

**Eval runner:** Iterates over scenario set, fires each one, captures trajectory, runs judge, produces a JSON report + summary stats.

---

## 11. Interview Talking Points (What This Proves)

1. **"Walk me through the architecture"** → Alert → Orchestrator → Specialist handoffs → Tool calls → HITL → Memory → Eval. Show the diagram.
2. **"Why multi-agent vs single agent?"** → Separation of concerns. Each agent has a focused system prompt + constrained tool set. Easier to eval, debug, and improve independently.
3. **"How do you handle safety?"** → No destructive tool exists without HITL gate. Tool-level enforcement, not prompt-level. The agent literally cannot bypass approval.
4. **"How does memory help?"** → On new incident, triage agent retrieves top-5 similar past incidents by symptom embedding. If a past incident with the same pattern was caused by a bad deploy, the agent skips straight to checking deploys. Measurably reduces MTTR.
5. **"How do you evaluate an agent that takes 10 steps?"** → Trajectory eval, not response eval. Score the full arc against ground truth. Show eval report with per-dimension scores.
6. **"What would you change for production?"** → Cosmos DB for memory (vector + JSON), Service Bus for decoupling, LangFuse for observability, Kong for token budgets, self-improvement loop for continuous learning. (Point at Phase 2 in TODO.md)
