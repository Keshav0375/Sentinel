# sentinel — Phase 2 Architecture Document

> **Purpose:** The sentinel repo IS the backend — it contains the multi-agent incident
> response pipeline (FastAPI + OpenAI Agents SDK), GHA orchestration workflows, and all
> CI/CD pipelines. Runs as an **ephemeral Docker container** inside GHA workflows —
> spun up on demand, validated, used, then torn down. No always-on hosting.
> PostgreSQL-backed memory, LLM routing via Anthropic/OpenAI, and LangFuse tracing.

---

## 1. System Overview

### 1.1 Ephemeral Backend — Run Per Incident, Not 24/7

The backend does NOT run on a server. Every incident response workflow:

1. **Pulls** the sentinel-backend Docker image from ACR
2. **Starts** it inside the GHA runner with secrets injected as env vars
3. **Validates** `/health` + `/ready` (DB connected, models reachable)
4. **Runs** the agent pipeline against the local container (`localhost:8000`)
5. **Tears down** the container when the workflow ends

**Why not AKS / always-on?** The pipeline runs maybe a few times per day during demos. Paying for a 24/7 node to serve a 5-minute job is waste. Running the container inside GHA costs zero additional compute (it uses the GHA runner's free minutes). PostgreSQL stays always-on (free tier) — only the backend is ephemeral.

```
┌─────────────────────────────────────────────────────────────────────┐
│                       sentinel repo                                 │
│                                                                      │
│  src/sentinel/          ← Backend code (agents, tools, memory, API) │
│  .github/workflows/     ← CI + CD + incident response workflows     │
│  tests/                 ← Unit + integration tests                   │
│  alembic/               ← Database migrations (replaces data/)      │
│  Planning/              ← Architecture docs, planning state          │
└───────────────────┬─────────────────────────────────────────────────┘
                    │
                    │  Docker build + push to ACR (on merge to main)
                    ▼
┌──────────────────────────────────────────────────────────────────┐
│                  GHA Runner (ubuntu-latest)                       │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │  docker run sentinel-backend:latest                      │     │
│  │  (ephemeral — started per incident, torn down after)     │     │
│  │                                                          │     │
│  │  FastAPI app (src/sentinel/main.py):                     │     │
│  │  ├── POST /webhooks/incident   (trigger run)             │     │
│  │  ├── GET  /incidents           (query history)           │     │
│  │  ├── GET  /incidents/{id}      (single)                  │     │
│  │  ├── POST /generate/pr-content (PR title+desc)           │     │
│  │  ├── GET  /eval/results        (eval dashboard)          │     │
│  │  ├── GET  /health              (liveness)                │     │
│  │  └── GET  /ready               (readiness)               │     │
│  │                                                          │     │
│  │  Agent pipeline:                                         │     │
│  │  Triage → Analysis → Resolution → Judge                  │     │
│  └─────────────────────────────────────────────────────────┘     │
│                                                                   │
│  Connects to:                                                     │
│  ├── Azure PostgreSQL B1MS (always-on, free 12 months)           │
│  ├── Anthropic API / OpenAI API (LLM calls)                      │
│  ├── Datadog API (log fetching)                                   │
│  ├── LangFuse Cloud (tracing)                                     │
│  └── GitHub API (PR details)                                      │
│                                                                   │
│  Secrets from: Azure Key Vault → injected as env vars             │
└──────────────────────────────────────────────────────────────────┘

Always-on Azure resources (free tier):
├── Azure PostgreSQL B1MS (free 12 months) — persistent data
├── Azure Container Registry (free 12 months) — Docker images
├── Azure Key Vault (always free) — secrets
├── Event Grid + Functions (always free) — alert routing
└── App Service F1 (always free) — sentinel-deployment target
```

### 1.2 What Stays Always-On vs Ephemeral

| Component | Always-on? | Why |
|-----------|-----------|-----|
| PostgreSQL | Yes | Data must persist between incidents. Free tier. |
| ACR | Yes | Images must be pullable anytime. Free tier. |
| Key Vault | Yes | Secrets must be fetchable at workflow start. Free tier. |
| Event Grid + Functions | Yes | Must receive Datadog alerts anytime. Free tier. |
| App Service (sentinel-deployment) | Yes | Must be deployable anytime. Free tier. |
| **sentinel-backend** | **No — ephemeral** | Runs inside GHA only when needed. Zero compute cost. |
| LangFuse | Yes (cloud) | Free tier, managed by LangFuse. |

---

## 2. End-to-End Incident Flow (with Correlation IDs)

Every boundary carries a correlation ID so the full arc is traceable from Datadog alert through agent pipeline to resolution.

```
1. Datadog detects deploy_status:failed on sentinel-deployment
   └── Generates: dd_event_id (Datadog event ID)

2. Datadog Monitor fires webhook → Azure Event Grid
   └── Payload includes: dd_event_id, alert_id, tags (service, deploy_status, version)

3. Event Grid → Azure Function (bridge)
   └── Function generates: correlation_id = uuid4()
   └── Passes through: dd_event_id, alert_id

4. Function → GHA repository_dispatch on sentinel repo
   └── client_payload: { correlation_id, dd_event_id, alert_id, tags, timestamp }

5. GHA orchestration workflow starts
   └── Job: fetch-context (parallel data gathering)
   │   ├── fetch-service-info    → service metadata from PostgreSQL
   │   ├── fetch-pr-details      → PR #, commit SHA, author from GitHub API
   │   └── fetch-datadog-logs    → recent error logs from Datadog Logs API
   │
   └── Job: trigger-pipeline
       └── POST /webhooks/incident with enriched payload + correlation_id

6. Agent pipeline runs on AKS (see §4 for agentic loop details)
   └── Triage → Analysis (with reflexion) → Decision point:

7. Decision: ROLLBACK or ESCALATE (based on root_cause_confidence + resolution_type)
   │
   ├── IF confidence ≥ 0.7 AND root cause maps to specific deploy:
   │   └── ROLLBACK PATH (backend decides, GHA executes)
   │       ├── Resolution agent outputs: target deploy SHA, rollback justification, evidence
   │       ├── Reverification confirms fix correctness → PASS
   │       ├── Backend returns resolution_type='rollback' with full context
   │       ├── GHA job: generate-pr-content → calls POST /generate/pr-content on backend
   │       ├── GHA job: create-rollback-pr → gh pr create on sentinel-deployment
   │       ├── GHA job: notify-rollback → Teams: "Revert PR #N created — review and merge"
   │       ├── PR = HITL gate. Reviewer merges → deploy.yml fires revert. Closes → no action.
   │       └── Judge scores the trajectory
   │
   └── IF confidence < 0.7 OR root cause is ambiguous/infra/third-party:
       └── ESCALATE PATH
           ├── Backend returns resolution_type='escalated' with full context
           ├── GHA job: notify-escalation → Teams: hypothesis, evidence, confidence, what's missing
           ├── No PR created — pipeline not confident enough to act
           ├── Judge scores the trajectory (escalation can still score well)
           └── Human takes over from Teams context

8. Always:
   └── Backend stores incident + trajectory to PostgreSQL, completes LangFuse trace
   └── GHA summary job: Datadog event + final Teams summary
```

### 2.1 Correlation Map

Every incident links to all related artifacts through the `deployments` table and incident metadata:

```
incident_id (UUID)
├── alert_id (Datadog alert ID — from webhook)
├── dd_event_id (Datadog event ID — from monitor)
├── correlation_id (UUID — generated at Event Grid bridge, carried through GHA → API)
├── deploy_id (UUID — from deployments table)
│   ├── pr_number (GitHub PR # that caused the deploy)
│   ├── commit_sha (git SHA of the deployed commit)
│   ├── deploy_gha_run_id (GHA run ID of the deployment workflow)
│   └── dd_deploy_event_id (Datadog custom event ID for the deploy)
└── langfuse_trace_id (LangFuse trace for the agent run)
```

This means: given any incident, you can trace back to the exact PR, the exact deploy, the exact Datadog logs, and the exact LLM trace.

---

## 3. API Contracts

### 3.1 Webhook Receiver

```
POST /webhooks/incident
Content-Type: application/json
X-Correlation-ID: {correlation_id}

{
  "source": "datadog",
  "alert_id": "evt-abc123",
  "dd_event_id": "dd-evt-456",
  "correlation_id": "uuid-from-bridge",
  "title": "Deploy FAILED for PR #5",
  "severity": "error",
  "tags": {
    "service": "dummy-api",
    "deploy_status": "failed",
    "failed_stage": "verify",
    "version": "pr-5-a3f9c2"
  },
  "context": {
    "service_metadata": { ... },
    "pr_details": { "number": 5, "sha": "a3f9c2", "author": "dev-bot", "files_changed": ["app/main.py"] },
    "recent_logs": [ ... ]
  },
  "timestamp": "2026-07-01T12:00:00Z",
  "raw_payload": { ... }
}

Response 202:
{
  "incident_id": "inc-uuid",
  "correlation_id": "uuid-from-bridge",
  "status": "accepted",
  "message": "Agent pipeline triggered"
}
```

The `context` field is pre-fetched by GHA parallel jobs. This means the agent pipeline starts with data already in hand — no cold-start data fetching.

### 3.2 Incident Query

```
GET /incidents?status=open&limit=10
GET /incidents/{id}
GET /incidents/{id}/trajectory   ← full agent trace
GET /incidents/{id}/deploy       ← linked deploy record
```

### 3.3 HITL — GitHub PR Review IS the Approval Gate

There is no `/approvals` endpoint. The revert PR on sentinel-deployment IS the HITL surface.

**Why not a custom approval API?** Because the destructive action is merging a revert PR. GitHub already has review → approve → merge. Building a parallel approval system would duplicate what GitHub does natively, and the reviewer would have to approve in two places (our API + the PR). Instead:

1. Resolution agent calls `draft_rollback_pr` → creates a real PR on sentinel-deployment via GitHub API
2. The PR description includes: incident ID, root cause, evidence summary, confidence score
3. Reviewer sees the PR, reviews the diff, approves/merges or closes
4. sentinel-deployment's `deploy.yml` fires on merge → deploys the revert
5. Sentinel backend watches for the PR merge event (via webhook or polling) → marks incident as `resolved`

If the reviewer closes the PR without merging → incident status moves to `escalated`, Teams notification sent.

**What about non-PR actions?** In Phase 2, the only destructive action is "revert a deploy" which always produces a PR. If we later add actions that don't map to PRs (restart a service, scale a pod), we'd add an approval endpoint then. Not now.

**Audit trail:** The `approvals` table still exists — it records that a revert PR was created, its URL, and whether it was merged or closed. This is the audit log, not the approval mechanism.

### 3.4 PR Content Generation (Specialized Agent Endpoint)

A dedicated endpoint that generates PR titles and descriptions for sentinel-deployment's demo PRs. Called by a GHA job in sentinel-deployment before the PR creation job.

**Why an agent and not a template?** Each demo PR introduces a specific failure mode (broken deps, health check 503, slow startup). The PR description needs to look like a real developer wrote it — realistic commit reasoning, plausible justification for the change, no hint that it's intentionally broken. A template can't do that. An LLM can write a convincing "I added retry config" description for a PR that actually breaks the health check.

```
POST /generate/pr-content
Content-Type: application/json

{
  "scenario": "break_health_check",
  "files_changed": ["app/main.py"],
  "diff_summary": "Changed /health endpoint to return 503 status",
  "failure_class": "health_check_failure",
  "pr_number": 5
}

Response 200:
{
  "title": "feat: add granular health status reporting",
  "description": "## Summary\n\nRefactored the health endpoint to report detailed component status instead of a simple OK. The endpoint now checks downstream dependencies and returns appropriate HTTP status codes based on overall system health.\n\n## Changes\n\n- Updated `/health` to return 503 when any component is degraded\n- Added dependency health aggregation logic\n\n## Testing\n\n- Verified locally with all deps running\n- Health endpoint returns 200 when all components are healthy",
  "model_used": "groq/llama-3.1-8b-instant",
  "tokens_used": 245
}
```

**Agent details:**

| Field | Value |
|-------|-------|
| Agent name | `pr_content_generator` |
| Model | `groq/llama-3.1-8b-instant` (fast — this is a generation task with tight constraints, not reasoning) |
| System prompt | `src/sentinel/agents/prompts/pr_content_generator.txt` |
| Tools | None — pure text generation |
| LangFuse | Traced as `pr-content-generation` span |

**System prompt key instructions:**
- Write as if you're a real developer who believes the change is correct
- Never mention that the change is intentional breakage or part of a test
- PR title must follow conventional commits (`feat:`, `fix:`, `refactor:`)
- Description must include Summary, Changes, and Testing sections
- Keep title under 72 characters
- Description should be 100-200 words

**Sentinel-deployment GHA usage (two-job pattern):**

```yaml
# In sentinel-deployment's create-demo-pr.yml workflow

jobs:
  generate-pr-content:
    runs-on: ubuntu-latest
    outputs:
      pr-title: ${{ steps.generate.outputs.title }}
      pr-description: ${{ steps.generate.outputs.description }}
    steps:
      - name: Generate PR content via sentinel backend
        id: generate
        run: |
          RESPONSE=$(curl -sf -X POST "${SENTINEL_API_URL}/generate/pr-content" \
            -H "Content-Type: application/json" \
            -d '{
              "scenario": "${{ matrix.scenario }}",
              "files_changed": ${{ toJSON(matrix.files) }},
              "diff_summary": "${{ matrix.diff_summary }}",
              "failure_class": "${{ matrix.failure_class }}",
              "pr_number": ${{ matrix.pr_number }}
            }')
          echo "title=$(echo $RESPONSE | jq -r '.title')" >> $GITHUB_OUTPUT
          echo "description=$(echo $RESPONSE | jq -r '.description')" >> $GITHUB_OUTPUT

  create-pr:
    needs: generate-pr-content
    runs-on: ubuntu-latest
    steps:
      - name: Checkout sentinel-deployment
        uses: actions/checkout@v4

      - name: Apply scenario changes
        run: |
          # Apply the file changes for this scenario
          # (e.g., modify app/main.py to return 503 from /health)

      - name: Create PR
        run: |
          git checkout -b "${{ matrix.branch_name }}"
          git add -A
          git commit -m "${{ needs.generate-pr-content.outputs.pr-title }}"
          git push origin "${{ matrix.branch_name }}"
          gh pr create \
            --title "${{ needs.generate-pr-content.outputs.pr-title }}" \
            --body "${{ needs.generate-pr-content.outputs.pr-description }}"
```

### 3.5 Health Probes

```
GET /health    → 200 {"status": "ok"}        (liveness)
GET /ready     → 200 {"status": "ready"}     (readiness — DB connected, models loaded)
                 503 {"status": "not_ready"}
```

---

## 4. Agent Pipeline — Agentic Loop Patterns

### 4.1 Architecture Decision: Hybrid Plan-Execute + Reflexion

The pipeline uses three agentic patterns, not just a linear chain:

1. **Plan-Execute** — The orchestrator creates a plan before dispatching agents. It doesn't blindly follow Triage → Analysis → Resolution. Instead, after triage, it evaluates what data is available and what's missing, then decides the next step.

2. **Reflexion (Self-Critique)** — After the Analysis agent produces a hypothesis, a reflexion step evaluates: "Is this hypothesis well-supported? What evidence is missing? Should I gather more data?" If confidence is low, the orchestrator loops back to fetch more logs or check more deploys.

3. **Reverification** — After the Resolution agent drafts a fix, a verification step checks: "Does this fix actually address the root cause identified? Are there side effects?" This prevents the pipeline from proposing a rollback for the wrong deploy.

### 4.2 Flow (with Loops)

```
Incoming webhook payload (enriched by GHA)
       │
       ▼
  ┌──────────────────┐
  │   ORCHESTRATOR   │  Plan: assess data, decide agent order
  │   (plan-execute) │  Model: groq/llama-3.3-70b-versatile
  └────────┬─────────┘
           │
           ▼
  ┌──────────┐
  │  TRIAGE  │  Classify severity, identify service, check duplicates
  │          │  Tools: get_service_metadata, search_past_incidents
  │          │  Model: groq/llama-3.1-8b-instant (fast classification)
  └────┬─────┘
       │
       ▼
  ┌──────────────┐
  │   ANALYSIS   │  Analyze logs + deploy data, form hypothesis
  │              │  Tools: fetch_logs, get_deploy_details
  │              │  Model: groq/llama-3.3-70b-versatile (reasoning)
  └────┬─────────┘
       │
       ▼
  ┌──────────────┐
  │  REFLEXION   │  Self-critique: Is the hypothesis well-supported?
  │  (internal)  │  If confidence < 0.7 → loop back to ANALYSIS with guidance
  │              │  Model: groq/llama-3.1-8b-instant (fast eval)
  │              │  Max loops: 2 (prevent infinite recursion)
  └────┬─────────┘
       │ (confidence ≥ 0.7 or max loops reached)
       ▼
  ┌──────────────────────────────────────────────────────────┐
  │  DECISION GATE — Orchestrator evaluates:                 │
  │  confidence ≥ 0.7 AND root cause = specific deploy?     │
  └────────┬─────────────────────────┬───────────────────────┘
           │ YES                     │ NO
           ▼                         ▼
  ┌──────────────┐          ┌──────────────┐
  │  RESOLUTION  │          │   ESCALATE   │
  │              │          │              │
  │  Prepare     │          │  Return full │
  │  rollback    │          │  context to  │
  │  spec:       │          │  GHA — not   │
  │  target SHA, │          │  confident   │
  │  justification│         │  enough to   │
  │  Model: 70b  │          │  act.        │
  └────┬─────────┘          └──────┬───────┘
       │                           │
       ▼                           │
  ┌──────────────────┐             │
  │  REVERIFICATION  │             │
  │  Spec matches    │             │
  │  root cause?     │             │
  │  Model: 8b       │             │
  └────┬──────┬──────┘             │
       │PASS  │FAIL/ESCALATE       │
       │      └────────────────────┤
       ▼                           │
  Return to GHA:                   │
  resolution_type='rollback'       │
  + rollback spec                  │
  (GHA creates the PR)             │
       │                           │
       ▼                           ▼
  ┌──────────────┐          ┌──────────────┐
  │    JUDGE     │          │    JUDGE     │
  │  Score full  │          │  Score full  │
  │  trajectory  │          │  trajectory  │
  │  Model: 8b   │          │  Model: 8b   │
  └────┬─────────┘          └──────┬───────┘
       │                           │
       ▼                           ▼
  Store to memory + Teams + LangFuse trace
```

**Key:** The Judge scores BOTH paths. An escalation that correctly gathered evidence and identified an ambiguous root cause scores well. A rollback that targeted the wrong deploy scores poorly. The Judge evaluates the *quality of the reasoning*, not the *outcome*.


### 4.3 Reflexion Implementation

Reflexion is NOT a separate agent — it's a critique step within the orchestrator's loop. After the Analysis agent returns a hypothesis, the orchestrator asks a reflexion prompt:

```
Given the hypothesis: "{hypothesis}"
And the evidence: {evidence_summary}

Rate your confidence (0.0-1.0) and explain:
1. What evidence supports this hypothesis?
2. What evidence is missing or contradictory?
3. What additional data would increase confidence?

If confidence < 0.7, specify exactly what tool call to make next.
```

The orchestrator evaluates the reflexion output. If confidence < 0.7 and loops < 2, it sends the Analysis agent back with the reflexion's guidance ("fetch logs for service X in the 10 minutes before the deploy, not after").

### 4.4 Reverification Implementation

After the Resolution agent proposes a fix, the orchestrator runs a verification check:

```
Proposed fix: {fix_description}
Root cause: {root_cause}
Target deploy: {deploy_id}, commit {commit_sha}

Verify:
1. Does this fix address the identified root cause?
2. Is the target deploy correct (timing, service, files)?
3. Could this fix introduce new issues?

Result: PASS (create revert PR) | FAIL (reason) | ESCALATE (too uncertain)
```

If PASS → `draft_rollback_pr` creates a real revert PR on sentinel-deployment. The PR IS the HITL gate — reviewer merges to deploy the fix.
If FAIL → the orchestrator can loop back to Resolution with correction guidance (max 1 retry).
If ESCALATE → no PR created. Teams notification with full context. Human decides.

### 4.5 Concurrency Model

Multiple incidents can run simultaneously. Each pipeline run is an independent `asyncio.Task` with its own short-term memory (Python dict keyed by `incident_id`).

```python
async def handle_incident(payload: IncidentPayload) -> None:
    incident_id = uuid4()
    short_term = ShortTermMemory(incident_id)
    
    langfuse_trace = langfuse.trace(
        name="incident-pipeline",
        id=str(incident_id),
        metadata={"correlation_id": payload.correlation_id}
    )
    
    result = await Runner.run(
        orchestrator,
        input=payload.model_dump_json(),
        context=RunContext(
            incident_id=incident_id,
            short_term=short_term,
            langfuse_trace=langfuse_trace,
        ),
    )
    
    await store_incident(result, short_term)
```

No locking needed — incidents are independent. Each gets its own DB connection from the asyncpg pool (pool_size=5, max 10). The B2ats node (2 vCPU, 4 GB) can handle 2-3 concurrent pipeline runs comfortably (each run uses ~500 MB peak during LLM calls).

### 4.6 Tool-Call Budget

The orchestrator enforces a hard cap of 20 tool calls per incident (up from 15 in Phase 1 to account for reflexion loops). If the budget is exhausted, the pipeline escalates to human with whatever context it has gathered so far.

### 4.7 Per-Agent Model Assignment

Not all tasks need the same model. Classification tasks need speed; reasoning tasks need depth.

**Providers:** Anthropic (default) and OpenAI only. No Groq, no Gemini. Top-tier models for a portfolio project — this is decision-making, not chat.

**Default provider: Anthropic.** Switch via `SENTINEL_PRIMARY_PROVIDER=openai` env flag. If the primary provider fails (rate limit, timeout, error), automatic fallback to the other provider.

| Agent / Step | Default (Anthropic) | Fallback (OpenAI) | Why |
|---|---|---|---|
| **Orchestrator** | `anthropic/claude-sonnet-4-6` | `openai/gpt-4o` | Plan-execute requires strong reasoning + tool use |
| **Triage** | `anthropic/claude-haiku-4-5` | `openai/gpt-4o-mini` | Fast classification — severity, service, dedup |
| **Analysis** | `anthropic/claude-sonnet-4-6` | `openai/gpt-4o` | Log pattern recognition, hypothesis formation — reasoning-heavy |
| **Reflexion** | `anthropic/claude-haiku-4-5` | `openai/gpt-4o-mini` | Confidence scoring is a simple eval task |
| **Resolution** | `anthropic/claude-sonnet-4-6` | `openai/gpt-4o` | Preparing rollback spec — reasoning-heavy |
| **Reverification** | `anthropic/claude-haiku-4-5` | `openai/gpt-4o-mini` | Binary pass/fail check |
| **Judge** | `anthropic/claude-haiku-4-5` | `openai/gpt-4o-mini` | Structured scoring with rubric |
| **PR Content Generator** | `anthropic/claude-haiku-4-5` | `openai/gpt-4o-mini` | Constrained text generation |

**Rationale:** Haiku for tasks where the prompt constrains the output space tightly (classify, score, pass/fail, templated generation). Sonnet for tasks where the model needs to reason across evidence, synthesize, and generate novel content.

**Fallback mechanism:**

```python
async def call_with_fallback(agent, input, primary, fallback):
    try:
        return await Runner.run(agent.with_model(primary), input)
    except (RateLimitError, TimeoutError, APIError):
        structlog.get_logger().warning("primary_provider_failed", provider=primary, falling_back_to=fallback)
        return await Runner.run(agent.with_model(fallback), input)
```

**Env config:**

```env
SENTINEL_PRIMARY_PROVIDER=anthropic       # 'anthropic' or 'openai'
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...                     # always required for fallback
```

**Cost note:** Both Anthropic and OpenAI are paid APIs. This is a portfolio project — the volume is low (a few incidents per demo session). Budget: ~$5-10/month for LLM calls during active development/demo.

### 4.8 Tool Changes from Phase 1

| Tool | Phase 1 (synthetic) | Phase 2 (real) |
|------|---------------------|----------------|
| `fetch_logs` | Generated logs | Datadog Logs API |
| `get_deploy_details` | Generated history | PostgreSQL `deployments` table + GitHub API |
| `prepare_rollback_spec` | Mock PR payload | Outputs target SHA, justification, evidence (GHA creates the actual PR) |
| `search_past_incidents` | Local SQLite | Azure PostgreSQL + pgvector |
| `get_service_metadata` | Local JSON seed | PostgreSQL `services` table |

Note: `draft_slack_summary` and `send_notification` are removed from the backend. Notifications are handled by GHA jobs (curl to Teams webhook). The backend focuses on reasoning, GHA handles execution.

---

## 5. Database — PostgreSQL

### 5.1 Migration from SQLite

| Concern | Phase 1 | Phase 2 |
|---------|---------|---------|
| Driver | `aiosqlite` | `asyncpg` |
| Connection | File path / `:memory:` | Connection string via pool |
| Embeddings | `numpy` cosine sim (full scan) | `pgvector` (IVFFlat indexed) |
| Migrations | Manual `CREATE TABLE` | `alembic` |
| Concurrency | Single connection | `asyncpg.Pool` (min=2, max=10) |

### 5.2 What PostgreSQL Stores (4 Tables)

| Table | Purpose | Key Queries |
|-------|---------|-------------|
| `incidents` | Every incident the system has handled — episodic memory | Similarity search by embedding, filter by status/service, trajectory replay |
| `services` | Service ownership, dependencies, runbooks — semantic memory | Lookup by name, similarity search for "which service matches this symptom?" |
| `revert_prs` | Revert PR lifecycle — created, merged, or closed | Filter by incident_id, track which PRs are pending review |
| `deployments` | Deploy metadata linking incidents to PRs, commits, and Datadog events | Correlate incident → deploy → PR → logs |

### 5.3 Schema

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- Episodic memory: every resolved incident
CREATE TABLE incidents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id TEXT NOT NULL,
    dd_event_id TEXT,
    correlation_id UUID,
    severity TEXT NOT NULL,
    service TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    root_cause TEXT,
    root_cause_confidence REAL,
    resolution TEXT,
    resolution_type TEXT,                -- 'rollback' | 'hotfix' | 'config_change' | 'escalated'
    trajectory JSONB,                    -- full agent trace (tool calls, handoffs, reflexion loops)
    eval_score JSONB,                    -- judge scores per dimension
    langfuse_trace_id TEXT,
    reflexion_loops INTEGER DEFAULT 0,   -- how many reflexion iterations ran
    created_at TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    mttr_seconds INTEGER,                -- mean time to resolution (computed on resolve)
    embedding VECTOR(384)                -- symptom embedding for similarity search
);

-- Semantic memory: service registry
CREATE TABLE services (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL,
    team TEXT,
    tier TEXT,                            -- 'critical' | 'standard' | 'best-effort'
    dependencies JSONB,                  -- ["service-a", "service-b"]
    runbook TEXT,                         -- markdown runbook content
    metadata JSONB,                      -- arbitrary service metadata
    embedding VECTOR(384)                -- service description embedding
);

-- Revert PR audit trail (HITL = GitHub PR review)
CREATE TABLE revert_prs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id UUID NOT NULL REFERENCES incidents(id),
    pr_number INTEGER NOT NULL,          -- PR # on sentinel-deployment
    pr_url TEXT NOT NULL,                -- full GitHub URL
    target_deploy_id UUID REFERENCES deployments(id),
    status TEXT NOT NULL DEFAULT 'open', -- 'open' | 'merged' | 'closed'
    reviewer TEXT,                       -- who merged/closed (from GitHub webhook)
    created_at TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ              -- when merged or closed
);

-- Deploy tracking: links incidents ↔ deploys ↔ PRs ↔ Datadog
CREATE TABLE deployments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service TEXT NOT NULL,
    pr_number INTEGER,
    commit_sha TEXT NOT NULL,
    author TEXT,
    deploy_status TEXT NOT NULL,          -- 'success' | 'failed' | 'rolled_back'
    gha_run_id BIGINT,                   -- GHA workflow run ID
    dd_deploy_event_id TEXT,             -- Datadog custom event ID for this deploy
    files_changed JSONB,                 -- list of changed files
    incident_id UUID REFERENCES incidents(id),  -- NULL if deploy succeeded
    deployed_at TIMESTAMPTZ DEFAULT now(),
    metadata JSONB
);

-- Indexes
CREATE INDEX idx_incidents_status ON incidents(status);
CREATE INDEX idx_incidents_service ON incidents(service);
CREATE INDEX idx_incidents_correlation ON incidents(correlation_id);
CREATE INDEX idx_incidents_embedding ON incidents USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);
CREATE INDEX idx_services_embedding ON services USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 5);
CREATE INDEX idx_deployments_service ON deployments(service);
CREATE INDEX idx_deployments_incident ON deployments(incident_id);
CREATE INDEX idx_deployments_commit ON deployments(commit_sha);
CREATE INDEX idx_revert_prs_incident ON revert_prs(incident_id);
```

### 5.4 Why These 4 Tables (Not More, Not Fewer)

- **incidents** = episodic memory. The agent learns from past incidents via vector similarity. Every field supports either query-time filtering or post-incident analysis.
- **services** = semantic memory. The triage agent looks up "which team owns this service?" and "what are its dependencies?" without an LLM call.
- **revert_prs** = HITL audit trail. Every revert PR the pipeline creates is tracked — its GitHub URL, which deploy it targets, and whether it was merged (approved) or closed (rejected). The PR itself IS the approval gate, not a custom API.
- **deployments** = the missing link. In Phase 1, deploy data was synthetic. In Phase 2, every real deploy to sentinel-deployment gets a row. When an incident fires, the agent can query: "what deployed to this service in the last 2 hours?" directly from PostgreSQL instead of making external API calls.

### 5.5 IVFFlat Index Sizing

With `lists = 10` for incidents and `lists = 5` for services, IVFFlat works well for < 10K rows. At our scale (maybe 100-500 incidents over the project lifetime), this is more than adequate. If scale grows, switch to HNSW (`CREATE INDEX ... USING hnsw`).

---

## 6. LangFuse Integration

### 6.1 What LangFuse Does for Sentinel

LangFuse is not just "logging LLM calls." It provides:

| Feature | How Sentinel Uses It |
|---------|---------------------|
| **Tracing** | Every incident pipeline run = 1 trace. Each agent handoff = 1 span. Each tool call = 1 generation. Full tree view of the entire pipeline. |
| **Prompt Management** | System prompts for all agents stored and versioned in LangFuse. Swap prompts without code deploy — pull from LangFuse API at startup. |
| **Scoring** | Judge agent's eval scores piped into LangFuse as trace-level scores. Dashboard shows score trends over time. |
| **Cost Tracking** | Token usage per agent, per incident, per day. Alerts if a single incident exceeds 50K tokens. |
| **Datasets** | Past incident trajectories exported as LangFuse datasets for regression testing. Run new prompt versions against historical incidents. |

### 6.2 SDK Integration Pattern

```python
from langfuse import Langfuse
from langfuse.decorators import observe, langfuse_context

langfuse = Langfuse()

@observe(name="incident-pipeline")
async def run_pipeline(payload: IncidentPayload) -> IncidentResult:
    langfuse_context.update_current_trace(
        metadata={"correlation_id": str(payload.correlation_id)},
        tags=["incident", payload.tags.service],
    )
    
    triage_result = await run_triage(payload)
    analysis_result = await run_analysis(triage_result)
    # ... etc

@observe(name="triage-agent")
async def run_triage(payload: IncidentPayload) -> TriageResult:
    # LangFuse automatically captures:
    # - Input/output
    # - Token usage
    # - Latency
    # - Model name
    ...

@observe(name="fetch-logs", as_type="tool")
async def fetch_logs(query: LogQuery) -> LogAnalysis:
    ...
```

### 6.3 Prompt Management

Instead of loading prompts from `src/sentinel/agents/prompts/*.txt`, Phase 2 loads them from LangFuse with a local file fallback:

```python
async def load_prompt(agent_name: str) -> str:
    try:
        prompt = langfuse.get_prompt(agent_name, cache_ttl_seconds=300)
        return prompt.compile()
    except Exception:
        return Path(f"src/sentinel/agents/prompts/{agent_name}.txt").read_text()
```

This means you can A/B test prompt changes without deploying code. LangFuse versioning shows which prompt version produced which scores.

### 6.4 Eval Pipeline → LangFuse Scores

After the Judge agent scores a trajectory, the scores are pushed to LangFuse:

```python
langfuse.score(
    trace_id=trace_id,
    name="triage_accuracy",
    value=judge_result.triage_accuracy,
)
langfuse.score(
    trace_id=trace_id,
    name="root_cause_correctness",
    value=judge_result.root_cause_correctness,
)
# ... one score per eval dimension
```

The LangFuse dashboard then shows: average scores over time, regression detection, per-model comparison.

---

## 7. Docker

### 7.1 Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini .

EXPOSE 8000

CMD ["uvicorn", "sentinel.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Use `PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu` for torch-cpu (~80 MB vs ~2 GB).

---

## 8. Ephemeral Backend Lifecycle (Inside GHA)

No Kubernetes. No AKS. The backend runs as a Docker container inside the GHA runner.

### 8.1 Start → Validate → Use → Teardown

```yaml
# Inside incident_response.yml — the start-backend job:

- name: Login to ACR
  run: az acr login --name ${{ secrets.ACR_NAME }}

- name: Start sentinel-backend container
  run: |
    docker run -d --name sentinel-backend \
      -p 8000:8000 \
      -e DATABASE_URL="${{ secrets.DATABASE_URL }}" \
      -e ANTHROPIC_API_KEY="${{ secrets.ANTHROPIC_API_KEY }}" \
      -e OPENAI_API_KEY="${{ secrets.OPENAI_API_KEY }}" \
      -e SENTINEL_PRIMARY_PROVIDER="${{ vars.SENTINEL_PRIMARY_PROVIDER }}" \
      -e LANGFUSE_PUBLIC_KEY="${{ secrets.LANGFUSE_PUBLIC_KEY }}" \
      -e LANGFUSE_SECRET_KEY="${{ secrets.LANGFUSE_SECRET_KEY }}" \
      -e DD_API_KEY="${{ secrets.DD_API_KEY }}" \
      ${{ secrets.ACR_NAME }}.azurecr.io/sentinel-backend:latest

- name: Wait for backend to start
  run: |
    for i in $(seq 1 30); do
      curl -sf http://localhost:8000/health && break
      sleep 2
    done

- name: Validate backend readiness
  run: |
    RESPONSE=$(curl -sf http://localhost:8000/ready)
    STATUS=$(echo "$RESPONSE" | jq -r '.status')
    if [ "$STATUS" != "ready" ]; then
      echo "::error::Backend not ready: $RESPONSE"
      docker logs sentinel-backend
      exit 1
    fi

# ... run pipeline jobs against http://localhost:8000 ...

- name: Teardown backend
  if: always()
  run: |
    docker logs sentinel-backend > backend.log 2>&1 || true
    docker stop sentinel-backend || true
    docker rm sentinel-backend || true
```

### 8.2 Why GHA Runner, Not AKS

| Concern | AKS (old plan) | GHA runner (new plan) |
|---------|----------------|----------------------|
| Cost | B2ats node: free 12 months, then ~$30/month | Zero — uses GHA free minutes |
| Uptime | 24/7 for a few-times-per-day job | On-demand: spin up in 10s, tear down in 2s |
| Secrets | K8s secrets + Key Vault sync | Direct from Key Vault → env vars |
| Networking | Ingress + TLS + public IP needed | localhost:8000 — no network exposure |
| Complexity | K8s manifests, cert-manager, ingress controller | `docker run` + `docker stop` |
| Docker experience | Dockerfile still needed (same) | Dockerfile still needed (same) |

**What we lose:** No always-on API endpoint for ad-hoc queries (`GET /incidents`). Acceptable — we can add a lightweight query-only mode later, or query PostgreSQL directly.

**What we gain:** Zero compute cost forever (not just 12 months), no K8s complexity, no ingress/TLS management, faster startup, simpler secrets flow.

---

## 9. CI/CD Workflows — Redesigned

All workflows live in `.github/workflows/` in this repo.

### 9.1 `ci_backend.yml` — Quality Gate on PR

**Trigger:** `pull_request` to `main` (paths: `src/**`, `tests/**`, `pyproject.toml`)

```
Jobs:
  quality:
    Steps: checkout → python 3.12 → install → ruff lint → ruff format → pyright → pytest (unit)

  integration:
    Needs: quality
    Services: pgvector/pgvector:pg16
    Steps: checkout → install → pytest (integration: test_agents/, test_api/)

  docker-build:
    Needs: quality
    Steps: checkout → docker build (verify, don't push)
```

### 9.2 `cd_backend.yml` — Build and Push Image on Merge

**Trigger:** `push` to `main` (paths: `src/**`, `Dockerfile`, `requirements*.txt`)

No deployment step — the image is pulled on-demand by `incident_response.yml`.

```
Jobs:
  build-push:
    Steps:
      - Checkout
      - Login to ACR
      - Docker build + push (tagged sha-{SHORT_SHA} + latest)
      - Report build result to Datadog (if: always())
```

### 9.3 `ci_incident.yml` — Agent Pipeline Smoke Test

**Trigger:** `pull_request` to `main` (paths: `src/sentinel/agents/**`, `src/sentinel/tools/**`)

```
Jobs:
  smoke:
    Services: pgvector/pgvector:pg16
    Steps:
      - Checkout → install → seed test DB
      - Run scenario: bad_deploy_01
      - Assert: incident created, root cause found, resolution proposed, judge score >= 0.6
```

### 9.4 `incident_response.yml` — The Real Pipeline (NEW)

**Trigger:** `repository_dispatch` (event_type: `incident-alert`)

This is the GHA workflow that runs the incident response pipeline. Triggered by the Azure Function bridge when Datadog detects a failure.

**Key design:** The backend does the reasoning (triage, analysis, reflexion, decision). GHA does the execution (PR creation, notifications). The backend returns a detailed response including `resolution_type` (rollback or escalated), and GHA jobs branch on that.

```
Job Flow:

  fetch-secrets
       │
       ▼
  start-backend            ← docker run from ACR + /health + /ready validation
       │
       ├──────────────────────────────┐
       ▼                              ▼                              ▼
  fetch-service-info          fetch-pr-details              fetch-datadog-logs
       │                              │                              │
       └──────────────┬───────────────┘──────────────────────────────┘
                      ▼
               run-agent-pipeline      ← POST /webhooks/incident + poll for result
                      │
                      │  response includes: resolution_type, root_cause, confidence, evidence
                      │
              ┌───────┴────────┐
              │                │
     resolution_type =    resolution_type =
       'rollback'           'escalated'
              │                │
              ▼                ▼
     generate-pr-content   notify-escalation
     (POST /generate/      (Teams: "needs
      pr-content)           human review"
              │             + full context)
              ▼                │
     create-rollback-pr        │
     (gh pr create on          │
      sentinel-deployment)     │
              │                │
              ▼                │
     notify-rollback           │
     (Teams: "Revert PR #N     │
      created — review         │
      and merge")              │
              │                │
              └───────┬────────┘
                      ▼
                   teardown-backend   ← docker stop + docker rm + save logs
                      ▼
                   summary            ← Datadog event + final Teams summary
```

```yaml
name: Incident Response Pipeline

on:
  repository_dispatch:
    types: [incident-alert]

jobs:
  # ─── Job 1: Fetch secrets from Key Vault ──────────────────────────
  fetch-secrets:
    runs-on: ubuntu-latest
    outputs:
      dd-api-key: ${{ steps.secrets.outputs.dd-api-key }}
      teams-webhook: ${{ steps.secrets.outputs.teams-webhook-url }}
    steps:
      - name: Azure Login
        uses: azure/login@v2
        with:
          creds: ${{ secrets.AZURE_CREDENTIALS }}
      - name: Fetch from Key Vault
        id: secrets
        uses: azure/get-keyvault-secrets@v1
        with:
          keyvault: sentinel-kv
          secrets: dd-api-key, teams-webhook-url

  # ─── Job 2: Start backend container ─────────────────────────────────
  start-backend:
    needs: fetch-secrets
    runs-on: ubuntu-latest
    steps:
      - name: Login to ACR
        run: az acr login --name ${{ secrets.ACR_NAME }}

      - name: Pull and start sentinel-backend
        run: |
          docker run -d --name sentinel-backend \
            -p 8000:8000 \
            -e DATABASE_URL="${{ secrets.DATABASE_URL }}" \
            -e ANTHROPIC_API_KEY="${{ secrets.ANTHROPIC_API_KEY }}" \
            -e OPENAI_API_KEY="${{ secrets.OPENAI_API_KEY }}" \
            -e SENTINEL_PRIMARY_PROVIDER="${{ vars.SENTINEL_PRIMARY_PROVIDER }}" \
            -e LANGFUSE_PUBLIC_KEY="${{ secrets.LANGFUSE_PUBLIC_KEY }}" \
            -e LANGFUSE_SECRET_KEY="${{ secrets.LANGFUSE_SECRET_KEY }}" \
            -e DD_API_KEY="${{ needs.fetch-secrets.outputs.dd-api-key }}" \
            ${{ secrets.ACR_NAME }}.azurecr.io/sentinel-backend:latest

      - name: Wait for startup + health check
        run: |
          for i in $(seq 1 30); do
            curl -sf http://localhost:8000/health && break
            sleep 2
          done

      - name: Validate readiness (DB connected, models reachable)
        run: |
          RESPONSE=$(curl -sf http://localhost:8000/ready)
          STATUS=$(echo "$RESPONSE" | jq -r '.status')
          if [ "$STATUS" != "ready" ]; then
            echo "::error::Backend not ready: $RESPONSE"
            docker logs sentinel-backend
            exit 1
          fi

  # ─── Jobs 3a/3b/3c: Parallel data gathering ──────────────────────
  fetch-service-info:
    needs: [fetch-secrets, start-backend]
    runs-on: ubuntu-latest
    outputs:
      service-metadata: ${{ steps.fetch.outputs.result }}
    steps:
      - name: Query service metadata from PostgreSQL
        id: fetch
        run: |
          SERVICE="${{ github.event.client_payload.tags.service }}"
          # psql query → output as JSON

  fetch-pr-details:
    needs: [fetch-secrets, start-backend]
    runs-on: ubuntu-latest
    outputs:
      pr-details: ${{ steps.fetch.outputs.result }}
    steps:
      - name: Get PR details from GitHub API
        id: fetch
        run: |
          VERSION="${{ github.event.client_payload.tags.version }}"
          PR_NUM=$(echo "$VERSION" | grep -oP 'pr-\K[0-9]+')
          # gh api repos/owner/sentinel-deployment/pulls/$PR_NUM

  fetch-datadog-logs:
    needs: [fetch-secrets, start-backend]
    runs-on: ubuntu-latest
    outputs:
      recent-logs: ${{ steps.fetch.outputs.result }}
    steps:
      - name: Fetch recent error logs from Datadog
        id: fetch
        env:
          DD_API_KEY: ${{ needs.fetch-secrets.outputs.dd-api-key }}
        run: |
          SERVICE="${{ github.event.client_payload.tags.service }}"
          # curl Datadog Logs API → last 30 min of errors

  # ─── Job 3: Run agent pipeline on backend ─────────────────────────
  run-agent-pipeline:
    needs: [fetch-secrets, fetch-service-info, fetch-pr-details, fetch-datadog-logs]
    runs-on: ubuntu-latest
    outputs:
      incident-id: ${{ steps.run.outputs.incident_id }}
      resolution-type: ${{ steps.run.outputs.resolution_type }}
      root-cause: ${{ steps.run.outputs.root_cause }}
      confidence: ${{ steps.run.outputs.confidence }}
      target-deploy: ${{ steps.run.outputs.target_deploy }}
      evidence-summary: ${{ steps.run.outputs.evidence_summary }}
      judge-scores: ${{ steps.run.outputs.judge_scores }}
    steps:
      - name: POST enriched payload to backend and poll for result
        id: run
        run: |
          # Merge: client_payload + service metadata + PR details + logs
          # POST to AKS: /webhooks/incident → get incident_id
          # Poll GET /incidents/$INCIDENT_ID every 15s, max 5 min
          # Extract all fields from response into outputs

  # ─── Job 4a: ROLLBACK PATH — generate PR content ──────────────────
  generate-pr-content:
    needs: run-agent-pipeline
    if: needs.run-agent-pipeline.outputs.resolution-type == 'rollback'
    runs-on: ubuntu-latest
    outputs:
      pr-title: ${{ steps.generate.outputs.title }}
      pr-description: ${{ steps.generate.outputs.description }}
    steps:
      - name: Call PR content generation agent
        id: generate
        run: |
          # POST /generate/pr-content with:
          #   scenario context, root cause, target deploy, evidence
          # Returns: title, description (realistic developer-style text)

  # ─── Job 4b: ROLLBACK PATH — create revert PR ─────────────────────
  create-rollback-pr:
    needs: [run-agent-pipeline, generate-pr-content]
    runs-on: ubuntu-latest
    outputs:
      pr-url: ${{ steps.create.outputs.pr_url }}
      pr-number: ${{ steps.create.outputs.pr_number }}
    steps:
      - name: Checkout sentinel-deployment
        uses: actions/checkout@v4
        with:
          repository: owner/sentinel-deployment
          token: ${{ secrets.GH_PAT }}

      - name: Create revert branch and PR
        id: create
        run: |
          TARGET_SHA="${{ needs.run-agent-pipeline.outputs.target-deploy }}"
          # git revert $TARGET_SHA
          # git push origin revert-$TARGET_SHA
          # gh pr create \
          #   --title "${{ needs.generate-pr-content.outputs.pr-title }}" \
          #   --body "${{ needs.generate-pr-content.outputs.pr-description }}"

  # ─── Job 4c: ROLLBACK PATH — notify Teams ─────────────────────────
  notify-rollback:
    needs: [run-agent-pipeline, create-rollback-pr, fetch-secrets]
    runs-on: ubuntu-latest
    steps:
      - name: Notify Teams — revert PR created, review needed
        run: |
          # POST to Teams webhook:
          # "Revert PR created — review and merge to deploy fix"
          # Include: PR URL, root cause, confidence, judge scores

  # ─── Job 5a: ESCALATION PATH — notify Teams directly ──────────────
  notify-escalation:
    needs: [run-agent-pipeline, fetch-secrets]
    if: needs.run-agent-pipeline.outputs.resolution-type == 'escalated'
    runs-on: ubuntu-latest
    steps:
      - name: Notify Teams — human intervention needed
        run: |
          # POST to Teams webhook:
          # "Pipeline escalated — not confident enough to act"
          # Include: hypothesis, evidence, confidence score, what's missing

  # ─── Job 6: Teardown backend (always runs) ─────────────────────────
  teardown-backend:
    needs: [run-agent-pipeline, notify-rollback, notify-escalation]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Save backend logs
        run: docker logs sentinel-backend > backend.log 2>&1 || true

      - name: Stop and remove container
        run: |
          docker stop sentinel-backend || true
          docker rm sentinel-backend || true

  # ─── Job 7: Summary (always runs) ─────────────────────────────────
  summary:
    needs: [run-agent-pipeline, teardown-backend]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Report to Datadog
        run: |
          # POST custom event to Datadog Events API:
          # { title: "Incident {resolved|escalated}",
          #   tags: ["service:X", "resolution:rollback|escalated"] }

      - name: Post final summary to Teams
        run: |
          # Full incident summary: severity, root cause, resolution type,
          # MTTR, judge scores, pipeline trace link (LangFuse)
```

**What the backend returns vs what GHA does:**

| Concern | Backend (agent pipeline) | GHA (orchestration) |
|---------|------------------------|---------------------|
| Triage, analysis, reflexion | Yes | No |
| Decision: rollback vs escalate | Yes (returns `resolution_type`) | Reads the decision, branches on it |
| PR title + description | Yes (via `/generate/pr-content`) | Calls the endpoint |
| Create actual PR | No | Yes (`gh pr create` on sentinel-deployment) |
| Teams notification | No | Yes (curl to webhook) |
| Datadog event reporting | No | Yes (curl to Events API) |
| Store incident to PostgreSQL | Yes (after pipeline completes) | No |
| LangFuse tracing | Yes | No |

### 9.5 Workflow Summary

| Workflow | Trigger | Purpose | Jobs |
|----------|---------|---------|------|
| `ci_backend.yml` | PR to main | Quality gate | quality → integration → docker-build |
| `cd_backend.yml` | Push to main | Build + push image | build → push ACR (no deploy — pulled on demand) |
| `ci_incident.yml` | PR to main (agents/) | Smoke test | seed → run scenario → assert |
| `incident_response.yml` | repository_dispatch | **Real pipeline** | secrets → start backend → parallel fetch → agent pipeline → branch (rollback PR / escalate) → notify → teardown → summary |

---

## 10. Terraform Justification

### Why Terraform (Not Azure CLI Scripts, Not Bicep, Not Manual)

| Alternative | Why Not |
|---|---|
| **Manual (Azure Portal)** | Can't reproduce. Can't version. Can't review. One click and state drifts. |
| **Azure CLI scripts** | Imperative — no state tracking. Can't diff what exists vs. what's desired. Idempotency is manual. |
| **Bicep** | Azure-only. Sentinel may expand to GCP/AWS later. Terraform is provider-agnostic. Also: Terraform experience is more transferable for interviews. |

### What Terraform Provisions (6 Modules)

AKS module removed — backend runs ephemerally inside GHA, no K8s needed.

| Module | Resources Created | Why It Exists |
|--------|------------------|---------------|
| `acr/` | Container Registry | Stores Docker images (pulled by GHA on demand) |
| `postgresql/` | Flexible Server + DB + pgvector extension + firewall | All persistent data |
| `keyvault/` | Key Vault + secrets + access policies | Centralized secret management |
| `event-grid/` | Topic + subscription (→ Function) | Routes Datadog alerts |
| `functions/` | Function App + storage + bridge code | Event Grid → GHA bridge |
| `app-service/` | App Service Plan (F1) + Web App | sentinel-deployment target |

One `terraform apply` creates the entire Sentinel infrastructure from zero. One `terraform destroy` tears it all down cleanly.

---

## 11. New Dependencies (Phase 2)

| Package | Purpose | Replaces |
|---------|---------|----------|
| `asyncpg` | Async PostgreSQL driver | `aiosqlite` |
| `pgvector` | pgvector Python bindings | `numpy` cosine sim |
| `alembic` | Database migrations | Manual CREATE TABLE |
| `langfuse` | LLM tracing, prompt mgmt, scoring | None (new) |
| `httpx` | Async HTTP (Datadog API, Teams webhook, GitHub API) | Already in Phase 1 |

---

## 12. Prerequisites & Setup Checklist

### Azure (provisioned by sentinel-infra)
- [ ] ACR with admin access or service principal
- [ ] PostgreSQL B1MS with pgvector extension enabled
- [ ] Key Vault with secrets: ANTHROPIC_API_KEY, OPENAI_API_KEY, DATABASE_URL, DD_API_KEY, TEAMS_WEBHOOK_URL, LANGFUSE_SECRET_KEY, LANGFUSE_PUBLIC_KEY

### External Services
- [ ] Anthropic API key (primary LLM provider)
- [ ] OpenAI API key (fallback LLM provider)
- [ ] Datadog API key + site
- [ ] LangFuse cloud account (free tier) — get public key + secret key
- [ ] Microsoft Teams incoming webhook URL

### Local Dev
- [ ] Docker Desktop
- [ ] `docker run pgvector/pgvector:pg16` for local PostgreSQL
- [ ] Python 3.12 + `pip install -e ".[dev]"`

---

## 13. Phase 1 → Phase 2 Migration Cleanup

Phase 1 has synthetic data generators, local SQLite, and scripts that won't exist in Phase 2. This section lists everything that gets removed, replaced, or rewritten.

### 13.1 Delete Entirely

| Path | Why It Goes |
|------|-------------|
| `data/` (entire directory) | Synthetic scenarios, seed JSON, seed.py — all replaced by real Datadog/GitHub/PostgreSQL data. No fake logs, no fake deploys, no fake alerts. |
| `data/scenarios/*.json` | 10 synthetic scenarios with hardcoded logs/deploys. Phase 2 tests against real pipeline with real data. |
| `data/services/service_map.json` | Service registry moves to PostgreSQL `services` table. Seed via alembic migration, not JSON file. |
| `data/services/dependency_graph.json` | Dependencies stored in `services.dependencies` JSONB column. |
| `data/services/runbooks.json` | Runbooks stored in `services.runbook` column. |
| `data/seed.py` | SQLite seeder. Replaced by alembic migrations + a seed migration for initial service data. |
| `data/sentinel.db` | SQLite database file. PostgreSQL replaces it entirely. |
| `src/sentinel/generator/` (entire directory) | Synthetic alert, log, and deploy generators. All 4 files (`alert_gen.py`, `log_gen.py`, `deploy_gen.py`, `scenarios.py`). Phase 2 gets real data from Datadog API and GitHub API. |
| `src/sentinel/infra/db.py` | SQLite connection management. Replaced by asyncpg pool in new `src/sentinel/infra/database.py`. |
| `scripts/run_scenario.py` | Fires synthetic scenarios. No synthetic scenarios in Phase 2. |
| `scripts/run_eval.py` | Runs eval against synthetic scenario set. Replaced by LangFuse-backed eval pipeline. |
| `scripts/demo.py` | Interactive demo with synthetic data. Replaced by real incident flow. |
| `docker-compose.yml` | Local dev used SQLite, no services needed. Phase 2 local dev uses `docker run pgvector/pgvector:pg16` directly. If we add a compose file later, it'll be a new one. |

### 13.2 Rewrite (Keep File, Replace Contents)

| Path | What Changes |
|------|-------------|
| `src/sentinel/memory/episodic.py` | SQLite + numpy cosine sim → asyncpg + pgvector queries. Same `MemoryStore` protocol, completely new implementation. |
| `src/sentinel/memory/semantic.py` | SQLite service lookup → asyncpg PostgreSQL queries. |
| `src/sentinel/memory/embeddings.py` | numpy cosine similarity → pgvector `<=>` operator. Embedding model stays (all-MiniLM-L6-v2). |
| `src/sentinel/infra/tracing.py` | Custom trace capture → LangFuse `@observe` decorators. |
| `src/sentinel/tools/log_fetcher.py` | `fetch_logs` returns synthetic data → calls Datadog Logs API via httpx. |
| `src/sentinel/tools/deploy_checker.py` | `list_recent_deploys` returns synthetic data → queries PostgreSQL `deployments` table + GitHub API. |
| `src/sentinel/tools/remediation_tools.py` | `draft_rollback_pr` returns mock payload → `prepare_rollback_spec` outputs target SHA + justification (GHA creates the PR). |
| `src/sentinel/tools/comms_tools.py` | Remove entirely — notifications handled by GHA jobs. |
| `src/sentinel/tools/incident_search.py` | `search_past_incidents` uses SQLite → uses asyncpg + pgvector similarity search. |
| `src/sentinel/tools/service_lookup.py` | `get_service_metadata` reads from JSON seed → queries PostgreSQL `services` table. |
| `src/sentinel/tools/hitl.py` | Remove entirely — HITL gate is the GitHub PR review, not a tool. |
| `src/sentinel/agents/orchestrator.py` | Linear chain → plan-execute with reflexion loops. |
| `src/sentinel/eval/judge.py` | Local JSON report → LangFuse scores. |
| `src/sentinel/eval/runner.py` | Iterates scenarios from `data/` → runs against LangFuse datasets. |
| `src/sentinel/config.py` | Add: DB connection string, Datadog API config, Teams webhook URL, LangFuse keys, asyncpg pool settings. |
| `Dockerfile` | Remove `COPY data/ ./data/`. No synthetic data in container. |

### 13.3 New Files (Phase 2 Only)

| Path | Purpose |
|------|---------|
| `src/sentinel/infra/database.py` | asyncpg pool management (replaces `db.py`) |
| `src/sentinel/agents/prompts/reflexion.txt` | Reflexion critique prompt |
| `src/sentinel/agents/prompts/reverification.txt` | Post-resolution verification prompt |
| `src/sentinel/agents/pr_content_generator.py` | PR content generation agent definition |
| `src/sentinel/agents/prompts/pr_content_generator.txt` | System prompt for PR title/description generation |
| `src/sentinel/api/generate.py` | `/generate/pr-content` endpoint |
| `alembic/` | Migration directory with `alembic.ini`, `env.py`, versions/ |
| `alembic/versions/001_initial_schema.py` | Creates 4 tables + pgvector extension |
| `alembic/versions/002_seed_services.py` | Inserts initial service data (replaces `data/seed.py`) |
| `.github/workflows/incident_response.yml` | Real incident pipeline workflow |

### 13.4 Dependencies to Remove

| Package | Why |
|---------|-----|
| `aiosqlite` | Replaced by `asyncpg` |
| `numpy` | Cosine similarity replaced by pgvector `<=>` operator. If sentence-transformers still needs it internally, keep as transitive dep only. |

### 13.5 Migration Order

Do the cleanup in this order to avoid broken imports:

1. **Add new deps** — `asyncpg`, `pgvector`, `alembic`, `langfuse` to `pyproject.toml`
2. **Create `database.py`** — asyncpg pool, parallel to old `db.py` (both exist briefly)
3. **Set up alembic** — `alembic init`, write initial migration
4. **Rewrite memory layer** — `episodic.py`, `semantic.py`, `embeddings.py` to use asyncpg
5. **Rewrite tools** — one at a time, each tool gets a PR
6. **Rewrite orchestrator** — add reflexion + reverification loops
7. **Add LangFuse** — tracing decorators, prompt loading, scoring
8. **Delete Phase 1 artifacts** — `data/`, `generator/`, `db.py`, old scripts, `aiosqlite` dep
9. **Update Dockerfile** — remove `COPY data/`
10. **Update CI** — add `incident_response.yml`, update `ci_incident.yml` to use real pipeline

Step 8 comes late intentionally — keep Phase 1 working until Phase 2 tools are proven.

---

## 14. Cost Breakdown

| Resource | Monthly Cost | Covered By |
|----------|-------------|------------|
| Backend compute | Free | Runs inside GHA runner — no separate compute |
| PostgreSQL B1MS | Free | 12-month free (750 hrs + 32 GB) |
| ACR Standard | Free | 12-month free (100 GB) |
| Key Vault | Free | Always free |
| Event Grid + Functions | Free | Always free |
| App Service F1 | Free | Always free |
| LangFuse | Free | Cloud free (50K obs/mo) |
| Datadog | Free | Student Pack (2 years) |
| GHA minutes | Free | GitHub Pro (3,000 min/mo) |
| Anthropic API | ~$5-10 | Paid — low volume (few incidents per demo) |
| OpenAI API (fallback) | ~$1-2 | Paid — only used when Anthropic fails |
| **Total** | **~$5-12/month** | **LLM costs only. Infrastructure is free.** |

**After 12 months:** PostgreSQL and ACR start costing ~$15-20/month. Total: ~$20-30/month.

**Compared to old AKS plan:** AKS would have added ~$30/month after free tier expired. Ephemeral approach saves that entirely.
