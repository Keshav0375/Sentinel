# sentinel — Phase 2 Architecture Document

> **↑ Deep dive of the [Architecture Index](../ARCHITECTURE.md).** Start there for the whole
> picture; this file is the authoritative detail for **backend** concerns (see the index's §4 map).

> **Purpose:** The sentinel repo IS the backend — it contains the multi-agent incident
> response pipeline (FastAPI + OpenAI Agents SDK), GHA orchestration workflows, and all
> CI/CD pipelines. Runs as a **single-replica Deployment on AKS** (free control plane +
> one B2ats_v2 node, free 12 months) — always-on, deployed by `ci_backend_deployment.yml`
> on every merge to main. PostgreSQL-backed memory, LLM routing via Anthropic/OpenAI,
> and LangFuse tracing.

---

## 1. System Overview

### 1.1 AKS-Hosted Backend — On-Demand, Single Replica (Scale-to-Zero)

The backend runs as a **single-replica Kubernetes Deployment on AKS** (free control
plane + one B2ats_v2 node). **The node pool idles at 0** — workflows scale it up
before using the backend and back down after (§8.5), so node-hours are consumed only
while Sentinel is actually working (~tens of hours/month instead of 730). It is
deployed on every merge to main by `ci_backend_deployment.yml`:

1. **Builds + pushes** the sentinel-backend image to ACR (tagged `sha-<short_sha>`)
2. **Scales up** the node pool if needed, syncs Key Vault secrets → K8s Secret
3. **Deploys**: `kubectl apply -f azure/k8s/` + `kubectl set image`, wait for rollout
4. **Validates** `/health` + `/ready`, then **tests** unit + integration + smoke against the live deployment
5. **Rolls back** (`kubectl rollout undo`) on failure, then **scales back to zero** (`if: always()`)

**Why AKS (single replica)?** The system needs a stable URL that every job in
`ci_incident_response.yml` can reach: GHA jobs run on separate fresh VMs, so a
container started in one job is invisible to the next — only a network-reachable
backend works. The LoadBalancer Service keeps its public IP across scale cycles, so
`BACKEND_URL` never changes. One replica is enough — incident volume is a few runs
per day, and in-process asyncio handles concurrent incidents (§4.5).

**The trade (accepted 2026-07-05):** a cold run pays ~3-7 min of node provisioning +
image pull before the pipeline starts. For live demos, set the repo variable
`SENTINEL_KEEP_WARM=true` to skip teardowns and pay the cold start once per session.

```
┌─────────────────────────────────────────────────────────────────────┐
│                       sentinel repo                                 │
│                                                                      │
│  src/sentinel/          ← Backend code — ALL reasoning lives here   │
│  azure/k8s/             ← Deployment + Service manifests             │
│  .github/workflows/     ← CI + deploy + incident response workflows │
│  .github/actions/       ← Reusable composite actions (see §9)       │
│  .claude/               ← Claude Code skills, commands, agents       │
│  tests/                 ← Unit + integration tests (mirrors src/)    │
│  alembic/               ← Database migrations (replaces data/)      │
│  Planning/              ← Architecture docs, planning state          │
└───────────────────┬─────────────────────────────────────────────────┘
                    │
                    │  ci_backend_deployment.yml (merge to main):
                    │  build → push to ACR → deploy to AKS → validate → test
                    ▼
┌──────────────────────────────────────────────────────────────────┐
│             AKS — 1× B2ats_v2 node (free 12 months)               │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │  Deployment: sentinel-backend (replicas: 1)              │     │
│  │  Image: sentinelacr.azurecr.io/sentinel-backend:sha-X    │     │
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
│  Service: LoadBalancer → public IP (dev). All non-health          │
│  endpoints require an Entra bearer token (aud api://sentinel-     │
│  backend), validated vs Entra JWKS — no shared secret (§3.6).     │
│                                                                   │
│  Connects to (via workload identity — no stored secret):          │
│  ├── Azure PostgreSQL B1MS (Entra token auth, free 12 months)    │
│  ├── Anthropic API / OpenAI API (keys read from Key Vault)       │
│  ├── Datadog API (log fetching)                                   │
│  ├── LangFuse Cloud (tracing)                                     │
│  └── GitHub API (PR details)                                      │
│                                                                   │
│  Identity: workload-identity ServiceAccount → Entra tokens for    │
│  Key Vault + PostgreSQL at runtime (no K8s Secret of app creds).  │
└──────────────────────────────────────────────────────────────────┘

Always-on Azure resources (free tier):
├── AKS control plane (always free) + 1× B2ats_v2 node (free 12 mo)
├── Azure PostgreSQL B1MS (free 12 months) — persistent data
├── Azure Container Registry (free 12 months) — Docker images
├── Azure Key Vault (always free) — secrets
├── Event Grid + Functions (always free) — alert routing
└── App Service F1 (always free) — sentinel-deployment target
```

### 1.2 What Runs Where

| Component | Always-on? | Why |
|-----------|-----------|-----|
| **sentinel-backend** | **No — on-demand (scale-to-zero)** | AKS node pool scaled 0↔1 by the workflows that need it (§8.5). Stable LB URL survives scale cycles. Preserves free B2ats_v2 hours for other projects. |
| PostgreSQL | Yes | Data must persist between incidents. Free tier. |
| ACR | Yes | Images must be pullable anytime. Free tier. |
| Key Vault | Yes | Secrets synced to AKS at deploy time + read by workflows. Free tier. |
| Event Grid + Functions | Yes | Must receive Datadog alerts anytime. Free tier. |
| App Service (sentinel-deployment) | Yes | Must be deployable anytime. Free tier. |
| LangFuse | Yes (cloud) | Free tier, managed by LangFuse. |

### 1.3 Repository Layout — Top-Level Buckets

One bucket per concern; nothing lives at the root except config files
(`pyproject.toml`, `Dockerfile`, `alembic.ini`, `CLAUDE.md`).

| Bucket | Path | What lives there |
|--------|------|------------------|
| Backend code | `src/sentinel/` | `agents/`, `tools/`, `memory/`, `api/`, `eval/`, `models/`, `providers/` (LLM provider routing), `infra/` (database, tracing, config). All reasoning. |
| Azure ops | `azure/` | `azure/k8s/` — Deployment + Service manifests. Any future az/kubectl helper scripts. Everything that describes what runs in Azure, without being Terraform (that's sentinel-infra's job). |
| CI/CD | `.github/` | `workflows/` (5 workflows) + `actions/` (reusable composite actions — the execution building blocks). |
| DB migrations | `alembic/` | Schema + seed migrations (`versions/`). |
| Tests | `tests/` | Mirrors `src/sentinel/` layout (`test_tools/`, `test_agents/`, `test_api/`, `test_models/`). |
| Planning | `Planning/` | Architecture docs, STATE, story reports. |
| Claude tooling | `.claude/` | Skills, commands, agents for the dev workflow. |

The bucket split enforces the core rule: **reasoning lives in `src/sentinel/`,
execution glue lives in `.github/`, runtime shape lives in `azure/`** — the file
system mirrors "backend reasons, GHA executes."

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
   │       ├── PR = HITL gate. Reviewer merges → ci_app_deployment.yml fires revert. Closes → no action.
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

> **Auth:** the backend is exposed on a public AKS LoadBalancer IP (dev). Every
> non-health endpoint requires an **Entra bearer token** — `Authorization: Bearer
> <jwt>` — validated against Entra JWKS (§3.6). No shared static secret exists
> (the old `X-Sentinel-Token`/`sentinel-api-token` is gone). Callers (GHA, a human
> on Swagger) mint a token scoped to `api://sentinel-backend`; requests without a
> valid token get 401, without the `Incident.Write` role get 403. `/health` and
> `/ready` stay open for K8s probes.

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
  "signal_type": "deploy_failure",   // deploy_failure (case ii) | runtime_error (case iii) — stamped by the bridge
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

**Two-case handling via `signal_type`** (from sentinel-deployment's 30 scenario
branches, §sentinel-deployment §4):

| `signal_type` | Case | Backend path |
|---------------|------|--------------|
| `deploy_failure` | ii — deploy failed, previous version still live | **Rollback fast path.** The offending deploy never went live; the orchestrator skips deep runtime diagnosis and goes straight to `prepare_rollback_spec` on the failed deploy's SHA (evidence = deploy event + CI logs). Still confidence-gated + judged. |
| `runtime_error` | iii — green deploy, live app breaking | **Full incident pipeline.** Triage → analysis → reflexion → resolution/escalation — correlate runtime error logs against the most-recent *successful* deploy (`deployments` table). |

The orchestrator reads `signal_type` in its plan step (§4.1) and picks the path.
Case i (clean pass) never reaches the backend — no monitor fires.

### 3.2 Incident Query

```
GET /incidents?status=open&limit=10
GET /incidents/{id}
GET /incidents/{id}/trajectory   ← full agent trace
GET /incidents/{id}/deploy       ← linked deploy record
```

### 3.3 HITL — GitHub PR Review IS the Approval Gate (Fire-and-Forget)

There is no `/approvals` endpoint and **no PR lifecycle tracking**. The revert PR on
sentinel-deployment IS the HITL surface, and Sentinel's job ends the moment the PR
exists and Teams has been notified.

**Why not a custom approval API?** Because the destructive action is merging a revert PR. GitHub already has review → approve → merge. Building a parallel approval system would duplicate what GitHub does natively, and the reviewer would have to approve in two places (our API + the PR). Instead:

1. Resolution agent outputs a rollback spec via `prepare_rollback_spec` (target SHA + justification) — no GitHub calls from the backend
2. GHA creates the revert PR; the description includes: incident ID, root cause, evidence summary, confidence score
3. GHA notifies Teams ("Revert PR #N created — review and merge") and posts the final summary
4. **Pipeline over.** The incident row is terminal at pipeline completion: `resolution_type='rollback'`, and GHA writes `pr_number` + `pr_url` onto the incident row for correlation

What the human does with the PR is entirely GitHub's domain: merging fires
`ci_app_deployment.yml` and deploys the revert; closing means no action. Sentinel does
NOT watch, poll, or record the PR outcome — no waiting state exists anywhere, and
Phase 2 has no requirement for reviewer/merge-outcome data. The PR itself, on GitHub,
is the audit trail. Consequence: `mttr_seconds` measures pipeline duration (alert
received → decision delivered), not human review latency — that's the metric we want
for evaluating the agent anyway.

**What about non-PR actions?** In Phase 2, the only destructive action is "revert a deploy" which always produces a PR. If we later add actions that don't map to PRs (restart a service, scale a pod), we'd add an approval endpoint then. Not now.

### 3.4 PR Content Generation (Rollback PR Only)

A dedicated endpoint that generates the **revert PR's title and description** during
an incident. It has exactly one consumer: the `generate-pr-content` job inside
`ci_incident_response.yml`, on the rollback path, immediately before
`create-rollback-pr`. This is used **only on the rollback path** (cases ii and
iii that resolve to a rollback). sentinel-deployment has no PR-generation workflow
— its scenarios are real git branches (§sentinel-deployment §4).

**Why an agent and not a template?** The revert PR is the HITL surface — a human
decides merge-or-close based on its description. That description must synthesize
incident-specific context (root cause, evidence, confidence, target deploy) into a
readable justification, and the synthesis differs per incident. A template can
interpolate fields; it can't summarize evidence.

```
POST /generate/pr-content
Content-Type: application/json
Authorization: Bearer {entra-jwt}

{
  "incident_id": "inc-uuid",
  "root_cause": "Deploy pr-5-a3f9c2 changed /health to return 503 when any dependency is degraded",
  "evidence_summary": "Verify stage failed 3/3 health checks post-deploy; Datadog logs show 503s starting at deploy time; no other deploys in the window",
  "confidence": 0.85,
  "target_deploy": { "pr_number": 5, "commit_sha": "a3f9c2", "deployed_at": "2026-07-01T12:00:00Z" },
  "resolution_type": "rollback"
}

Response 200:
{
  "title": "revert: roll back PR #5 — health endpoint 503s (incident inc-uuid)",
  "description": "## Incident\n\nSentinel detected failed health checks on dummy-api starting 12:02 UTC...\n\n## Root Cause\n\nPR #5 (a3f9c2) changed /health to return 503 when any dependency is degraded...\n\n## Evidence\n\n- 3/3 verify health checks failed post-deploy\n- 503s in Datadog logs begin exactly at deploy time\n\n## Rollback\n\nThis PR reverts a3f9c2. Confidence: 0.85. Merge to deploy the fix; close to reject and handle manually.",
  "model_used": "anthropic/claude-haiku-4-5",
  "tokens_used": 245
}
```

**Agent details:**

| Field | Value |
|-------|-------|
| Agent name | `pr_content_generator` |
| Model | `anthropic/claude-haiku-4-5` (fallback `openai/gpt-4o-mini`) — generation with tight constraints, not reasoning |
| System prompt | `src/sentinel/agents/prompts/pr_content_generator.txt` |
| Tools | None — pure text generation |
| LangFuse | Traced as `pr-content-generation` span |

**System prompt key instructions:**
- Title: conventional-commit `revert:` prefix, include PR # and incident ID, under 72 characters
- Description sections: Incident, Root Cause, Evidence, Rollback (what merging does, what closing means)
- State the confidence score plainly — the reviewer weighs it
- Write for the human deciding merge vs close: factual, specific, no hedging, no drama
- 100-200 words

**Consumer (the only one) — `ci_incident_response.yml` rollback path:**

```yaml
  generate-pr-content:
    needs: run-agent-pipeline
    if: needs.run-agent-pipeline.outputs.resolution-type == 'rollback'
    steps:
      - # POST $BACKEND_URL/generate/pr-content (Authorization: Bearer) with the
        # pipeline outputs: incident_id, root_cause, evidence, confidence, target deploy
        # → outputs: pr-title, pr-description → consumed by create-rollback-pr
```

### 3.5 Health Probes

```
GET /health    → 200 {"status": "ok"}        (liveness)   ← OPEN, no auth
GET /ready     → 200 {"status": "ready"}     (readiness — DB connected, models loaded)  ← OPEN
                 503 {"status": "not_ready"}
```

These two are the **only** unauthenticated endpoints — K8s kubelet probes can't
carry a bearer token, and load balancers must probe freely.

### 3.6 Inbound Auth — Entra Bearer Validation (FastAPI dependency)

Every non-health route depends on `require_incident_write`, a FastAPI dependency
that validates the incoming JWT **statelessly** against Entra's public keys — no
shared secret, no DB lookup.

```python
# src/sentinel/api/auth.py  (new)
from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt  # PyJWT + jwt.PyJWKClient

_JWKS = jwt.PyJWKClient(f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys")

async def require_incident_write(
    creds: HTTPAuthorizationCredentials = Security(HTTPBearer()),
) -> dict:
    try:
        key = _JWKS.get_signing_key_from_jwt(creds.credentials).key
        claims = jwt.decode(
            creds.credentials, key, algorithms=["RS256"],
            audience="api://sentinel-backend",
            issuer=f"https://sts.windows.net/{TENANT_ID}/",   # v1; use login.microsoftonline.com/<tid>/v2.0 for v2
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "invalid token") from exc
    if "Incident.Write" not in claims.get("roles", []):
        raise HTTPException(403, "missing Incident.Write role")
    return claims
```

- **Config, not secrets:** `TENANT_ID` and the audience `api://sentinel-backend`
  are public identifiers — supplied as env from GitHub *variables* / pod env,
  never Key Vault.
- **What's checked:** signature (RS256 against JWKS), `aud`, `iss`, `exp`, and the
  `roles` claim contains `Incident.Write`.
- **One identity, many tokens:** the caller (GHA `sentinel-gha` SP, or a human)
  obtains this token via `az account get-access-token --resource api://sentinel-backend`
  — a *different* token from the ARM/DB tokens the same identity uses elsewhere
  (see sentinel-infra §4.5).
- **Swagger:** declaring `HTTPBearer` renders an **Authorize** button in `/docs`,
  so manual verification attaches the token to requests automatically.

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
  │   (plan-execute) │  Model: Sonnet (reasoning — see §4.7)
  └────────┬─────────┘
           │
           ▼
  ┌──────────┐
  │  TRIAGE  │  Classify severity, identify service, check duplicates
  │          │  Tools: get_service_metadata, search_past_incidents
  │          │  Model: Haiku (fast classification — see §4.7)
  └────┬─────┘
       │
       ▼
  ┌──────────────┐
  │   ANALYSIS   │  Analyze logs + deploy data, form hypothesis
  │              │  Tools: fetch_logs, get_deploy_details
  │              │  Model: Sonnet (reasoning — see §4.7)
  └────┬─────────┘
       │
       ▼
  ┌──────────────┐
  │  REFLEXION   │  Self-critique: Is the hypothesis well-supported?
  │  (internal)  │  If confidence < 0.7 → loop back to ANALYSIS with guidance
  │              │  Model: Haiku (fast eval — see §4.7)
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
  │  Model:Sonnet│          │  act.        │
  └────┬─────────┘          └──────┬───────┘
       │                           │
       ▼                           │
  ┌──────────────────┐             │
  │  REVERIFICATION  │             │
  │  Spec matches    │             │
  │  root cause?     │             │
  │  Model: Haiku    │             │
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
  │  Model: Haiku│          │  Model: Haiku│
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

If PASS → the backend returns `resolution_type='rollback'` with the rollback spec from `prepare_rollback_spec`; GHA creates the revert PR on sentinel-deployment. The PR IS the HITL gate — reviewer merges to deploy the fix.
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

No locking needed — incidents are independent. Each gets its own DB connection from the asyncpg pool (min=2, max=10). The single backend replica on the B2ats_v2 node (2 vCPU, 4 GB) handles 2-3 concurrent pipeline runs comfortably (each run uses ~500 MB peak during LLM calls) — concurrent incidents arrive as separate `ci_incident_response.yml` runs all POSTing to the same always-on backend.

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

### 5.2 What PostgreSQL Stores (3 Tables)

| Table | Purpose | Key Queries |
|-------|---------|-------------|
| `incidents` | Every incident the system has handled — episodic memory. Carries `pr_number`/`pr_url` when a revert PR was created (creation record only — no lifecycle tracking, see §3.3) | Similarity search by embedding, filter by status/service, trajectory replay |
| `services` | Service ownership, dependencies, runbooks — semantic memory | Lookup by name, similarity search for "which service matches this symptom?" |
| `deployments` | Deploy metadata linking incidents to PRs, commits, and Datadog events. **Written by `ci_app_deployment.yml`'s record-deployment stage on every deploy (success and failure)** | Correlate incident → deploy → PR → logs; "what deployed to this service in the last 2 hours?" |

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
    pr_number INTEGER,                   -- revert PR # (written by GHA right after PR creation)
    pr_url TEXT,                         -- revert PR URL (creation record only — outcome not tracked)
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

-- Deploy tracking: links incidents ↔ deploys ↔ PRs ↔ Datadog
-- Rows inserted by ci_app_deployment.yml (record-deployment stage) on every deploy.
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
    incident_id UUID REFERENCES incidents(id),  -- NULL at insert; backfilled by the backend when an incident correlates to this deploy
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
```

### 5.4 Why These 3 Tables (Not More, Not Fewer)

- **incidents** = episodic memory. The agent learns from past incidents via vector similarity. Terminal state is written at pipeline completion; if a revert PR was created, GHA writes `pr_number` + `pr_url` onto the incident row (one `psql UPDATE`) for correlation.
- **services** = semantic memory. The triage agent looks up "which team owns this service?" and "what are its dependencies?" without an LLM call.
- **deployments** = the missing link. In Phase 1, deploy data was synthetic. In Phase 2, every real deploy to sentinel-deployment gets a row — inserted by `ci_app_deployment.yml`'s record-deployment stage, for failed deploys too (those are exactly the ones incidents correlate with). When an incident fires, the agent can query: "what deployed to this service in the last 2 hours?" directly from PostgreSQL instead of making external API calls.

There is no `revert_prs` table — HITL is fire-and-forget (§3.3). The PR itself, on GitHub, is the approval surface and the audit trail; Sentinel does not track its lifecycle.

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

## 8. Backend Deployment on AKS

The backend runs as a single-replica Kubernetes Deployment on AKS (cluster provisioned
by sentinel-infra's `aks/` module). Manifests live in `azure/k8s/` in this repo and are
applied by `ci_backend_deployment.yml` on every merge to main.

### 8.1 Manifests

| File | Purpose |
|------|---------|
| `azure/k8s/deployment.yaml` | Deployment, `replicas: 1`, image from ACR, `serviceAccountName: sentinel-backend` + workload-identity label, `envFrom` the **non-secret** `sentinel-config` ConfigMap, liveness `/health`, readiness `/ready` |
| `azure/k8s/serviceaccount.yaml` | ServiceAccount annotated `azure.workload.identity/client-id: <backend UAMI client id>` (from the infra output) — federates the pod to Entra (§sentinel-infra §3.7) |
| `azure/k8s/service.yaml` | `Service type: LoadBalancer` — created at backend-up, **deleted at backend-down** (releases the public IP → $0 idle). The URL is resolved fresh each run (§8.5). |

```yaml
# azure/k8s/deployment.yaml (core)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: sentinel-backend
spec:
  replicas: 1
  strategy: { type: Recreate }   # 1 replica on a 4 GB node — never two pods during rollout
  selector:
    matchLabels: { app: sentinel-backend }
  template:
    metadata:
      labels:
        app: sentinel-backend
        azure.workload.identity/use: "true"   # opt the pod into workload identity
    spec:
      serviceAccountName: sentinel-backend     # annotated with the backend UAMI client-id
      containers:
        - name: sentinel-backend
          image: sentinelacr.azurecr.io/sentinel-backend:sha-PLACEHOLDER  # set by CI via kubectl set image
          ports: [{ containerPort: 8000 }]
          envFrom:
            - configMapRef: { name: sentinel-config }   # NON-secret config only (see §8.2)
          readinessProbe:
            httpGet: { path: /ready, port: 8000 }
            initialDelaySeconds: 10
          livenessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 15
          resources:
            requests: { cpu: 250m, memory: 512Mi }
            limits: { cpu: "1", memory: 2Gi }
---
# azure/k8s/service.yaml
apiVersion: v1
kind: Service
metadata:
  name: sentinel-backend
spec:
  type: LoadBalancer
  selector: { app: sentinel-backend }
  ports: [{ port: 80, targetPort: 8000 }]
```

### 8.2 No K8s Secret — Workload Identity at Runtime

The pod holds **no application credentials**. It runs under the `sentinel-backend`
ServiceAccount (federated to the backend UAMI, §sentinel-infra §3.7) and fetches
what it needs from Entra *at runtime* via `DefaultAzureCredential` /
`WorkloadIdentityCredential`:

- **LLM + LangFuse keys** → read straight from Key Vault (`azure-keyvault-secrets`
  SDK) — always the latest (rotated) version.
- **PostgreSQL** → mint an Entra DB token (scope
  `https://ossrdbms-aad.database.windows.net/.default`) and use it as the psql
  password; refresh before the ~24 h managed-identity token expires.

The deploy workflow only applies a **non-secret** ConfigMap:

```bash
az aks get-credentials --resource-group sentinel-rg --name sentinel-aks

kubectl create configmap sentinel-config \
  --from-literal=SENTINEL_PRIMARY_PROVIDER="anthropic" \
  --from-literal=AZURE_TENANT_ID="<tenant-id>" \
  --from-literal=SENTINEL_API_AUDIENCE="api://sentinel-backend" \
  --from-literal=KEY_VAULT_URL="https://sentinel-kv.vault.azure.net" \
  --from-literal=PGHOST="sentinel-pg.postgres.database.azure.com" \
  --from-literal=PGDATABASE="sentinel" \
  --from-literal=PGUSER="sentinel-backend-wi" \
  --dry-run=client -o yaml | kubectl apply -f -
```

Nothing here is a secret — all identifiers/config. Rotating an LLM key = the
rotation Function writes a new version (§sentinel-infra §3.8); the pod picks it up
on its next read, **no redeploy**.

### 8.3 Image Pull: AcrPull Role, No imagePullSecrets

Terraform grants the AKS kubelet identity the `AcrPull` role on ACR — pods pull
images without registry credentials living in the cluster.

### 8.4 Why AKS (vs Ephemeral Container Inside GHA)

The previous plan ran the backend as a Docker container inside the GHA runner. That
design had a fatal flaw plus real functional gaps:

| Concern | Ephemeral in GHA (old plan) | AKS scale-to-zero (current) |
|---------|----------------------------|------------------------------|
| Cross-job access | **Broken** — each GHA job runs on a fresh runner VM; a container started in `start-backend` doesn't exist for `run-agent-pipeline` | Stable LB URL reachable from every job — the URL survives scale cycles |
| `/generate/pr-content` (rollback PR content, mid-incident) | Same broken cross-job problem | Reachable from any job in the incident workflow |
| Ad-hoc queries (`GET /incidents`) | Not available | Available while scaled up (or via `ci_backend_scale.yml up`) |
| Cold start | Pull image + start + validate every run, per job | ~3-7 min per cold run (node + image pull); zero within a `KEEP_WARM` session |
| Cost | $0 | Node-hours only while scaled up (~20-80 hrs/mo of the free 750) |
| Exposure | localhost only | Public LB IP — mitigated by **Entra bearer auth** (aud `api://sentinel-backend`, role `Incident.Write`) on all non-health endpoints (§3.6) |

Trade-off accepted: we take on K8s manifests, a public endpoint, and per-run scaling
logic in exchange for a working multi-workflow orchestration model at near-zero
node-hour consumption. Complexity is capped deliberately: single replica, no
autoscaling, no ingress controller, no TLS termination (dev), no service mesh.

### 8.5 Scale-to-Zero Lifecycle (On-Demand Backend)

The node pool idles at 0. Every workflow that needs the backend brings it up and
tears it down via two composite actions shared across workflows:

```
.github/actions/backend-up/action.yml          (outputs: backend-url)
  az aks nodepool scale --resource-group sentinel-rg --cluster-name sentinel-aks \
    --name default --node-count 1          # no-op if already 1
  kubectl apply -f azure/k8s/               # idempotent — recreates Service if deleted
  kubectl scale deployment/sentinel-backend --replicas=1
  kubectl rollout status deployment/sentinel-backend --timeout=600s
  # Resolve the URL from the cluster — there is NO static URL variable:
  for i in $(seq 1 60); do
    IP=$(kubectl get svc sentinel-backend \
      -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
    [ -n "$IP" ] && break; sleep 5          # Azure LB provisioning ~1-3 min when cold
  done
  poll http://$IP/health + /ready           # up to 10 min total (cold pull ~1-2 GB)
  echo "backend-url=http://$IP" >> "$GITHUB_OUTPUT"

.github/actions/backend-down/action.yml
  skipped entirely when vars.SENTINEL_KEEP_WARM == 'true'   # demo mode
  kubectl delete -f azure/k8s/service.yaml --ignore-not-found   # releases the public IP → $0 idle
  kubectl scale deployment/sentinel-backend --replicas=0
  az aks nodepool scale ... --node-count 0
```

**Dynamic URL — no `SENTINEL_BACKEND_URL` variable.** The only consumers of the
backend are jobs inside these workflows, and they all have OIDC + kubectl access, so
the URL is resolved from the cluster at run time: `backend-up` emits it as the
`backend-url` output, the scaling job exposes it as a **job output** (it's not a
secret, so job outputs are legal), and downstream jobs read
`needs.<scale-job>.outputs.backend-url`. Each cold run gets a fresh public IP; in a
`KEEP_WARM` session the Service (and its IP) survives, so warm runs resolve the same
address instantly.

**Rules that make per-run scaling safe:**

1. **Serialization.** Every backend-using workflow (`ci_incident_response.yml` and
   `ci_backend_deployment.yml` — sentinel-deployment's `ci_app_deployment.yml`
   never touches the backend) declares:
   ```yaml
   concurrency:
     group: sentinel-backend
     cancel-in-progress: false
   ```
   Runs queue instead of overlapping — one run's teardown can never kill another
   run's backend. GHA caveat: only ONE run waits per group; a third simultaneous
   trigger is auto-cancelled (visible in the Actions tab). Acceptable at demo
   cadence — incidents originate from your own merges, one at a time.
2. **Teardown is `if: always()`** — a failed pipeline still scales back to zero.
3. **Demo mode.** Set repo variable `SENTINEL_KEEP_WARM=true` for a live session:
   teardowns are skipped, the cold start is paid once per session. Flip it back
   after — or let the safety net catch it.
4. **Safety net.** `ci_backend_scale.yml`: `workflow_dispatch` (input: `up`/`down`)
   for manual control, plus a nightly cron (02:00 UTC) that scales everything to 0.
   A forgotten `KEEP_WARM` session costs at most one night of node-hours.

**Cost of the trade:** every cold run pays ~3-7 min of node spin-up plus ~1-3 min of
LB provisioning before the pipeline starts. Wall-clock incident response includes it;
backend-computed `mttr_seconds` does not (it's measured from webhook receipt by the
backend). With the Service deleted at teardown, the public IP is released too —
**idle cost is genuinely $0**; the IP bills only for scaled-up hours (cents/month).

---

## 9. CI/CD Workflows — Redesigned

All workflows live in `.github/workflows/` in this repo.

### Workflow Naming Convention (All Repos)

All workflow files use the `ci_` prefix for consistency. Naming pattern: `ci_<descriptive_scope>.yml`.

| File | Repo | Purpose |
|------|------|---------|
| `ci_validation.yml` | sentinel | Fast PR gate — lint, typecheck, Docker build (no push), run backend locally, health check, unit + integration tests |
| `ci_backend_deployment.yml` | sentinel | Deploy on merge — lint, typecheck, build + push image to ACR, scale up AKS, deploy, validate rollout, all tests against the live deployment, smoke test, rollback on failure, ACR cleanup, scale to zero |
| `ci_incident_response.yml` | sentinel | Real incident pipeline (repository_dispatch) — scales the backend up, runs, scales it down (§8.5) |
| `ci_backend_scale.yml` | sentinel | Manual `up`/`down` toggle (workflow_dispatch) + nightly auto-down cron — scale-to-zero safety net |
| `ci_app_deployment.yml` | sentinel-deployment | Build → Deploy → Verify → Record deploy in PostgreSQL → Report to Datadog. Deploys one of the 30 scenario branches (§sentinel-deployment §4) |
| `ci_infra_dry.yml` | sentinel-infra | Terraform validate + plan (dry run, never applies) |
| `ci_infra.yml` | sentinel-infra | Terraform apply (merge to main only) |
| `ci_destroy_infra.yml` | sentinel-infra | Manual full teardown — `terraform destroy` + `az group delete` (§sentinel-infra §7.3) |
| `ci_runners.yml` | sentinel-infra | Build + push CI runner images to ACR |

**Workflow `name:` field:** `[repo] scope — description`
- Examples: `[sentinel] backend — quality gate`, `[infra] terraform — apply`, `[deployment] deploy — build and ship`

**Job IDs:** `kebab-case` verb-noun (e.g., `run-quality`, `build-and-push`, `deploy-to-aks`)

**Job `name:` field:** Title case, brief (e.g., `Run Lint`, `Build Image`, `Fetch Secrets`)

This convention applies across all three repos: sentinel, sentinel-infra, sentinel-deployment.

### Reusable Composite Actions (`.github/actions/`)

Repeated step sequences are factored into composite actions — defined once, versioned
with the repo, used by every workflow. Rule: **if a step block appears in two places,
it becomes an action.**

| Action | Inputs → Outputs | Used By |
|--------|------------------|---------|
| `backend-up` | — → `backend-url` | `ci_incident_response` (ensure-backend-up), `ci_backend_deployment` (deploy-to-aks), `ci_backend_scale` (up) |
| `backend-down` | — | `ci_incident_response` (teardown-backend), `ci_backend_deployment` (promote-or-rollback), `ci_backend_scale` (down + nightly cron) |
| `get-kv-secrets` | `names` (list) → one output per secret, each `::add-mask::`ed | Every job that reads a runtime secret (Datadog/Teams/LangFuse), in both repos |
| `get-db-token` | — → `pg-token` (Entra DB token, `::add-mask::`ed) | Any job that runs psql — replaces the old `db-password` fetch |
| `get-backend-token` | — → `bearer` (aud `api://sentinel-backend`, masked) | run-agent-pipeline, generate-pr-content, backend smoke tests |
| `notify-teams` | `title`, `body`, `severity`, `webhook-url` | notify-rollback, notify-escalation, summary, backend-up failure alerts, deploy-rollback alert |
| `psql-exec` | `sql`, `pg-token` → `result` (JSON) | fetch-service-info (read), create-rollback-pr (UPDATE incidents), sentinel-deployment's record-deployment (INSERT) |

sentinel-deployment defines one local action of its own — `dd-report` (`title`,
`tags`, `alert-type`, optional log payload) — used by every reporting stage of
`ci_app_deployment.yml`.

**Cross-repo reuse:** sentinel-deployment references sentinel's shared actions
directly (`uses: Keshav0375/Sentinel/.github/actions/psql-exec@main`). Works out of the
box for public repos; for private repos, enable Actions access for repositories owned
by the same owner in sentinel's settings.

### 9.1 `ci_validation.yml` — Fast PR Gate

**Name:** `[sentinel] PR — validation`

**Trigger:** `pull_request` to `main` (paths: `src/**`, `tests/**`, `pyproject.toml`, `Dockerfile`, `azure/**`)

Lightweight check on every PR open/sync. Catches code quality issues and verifies the Docker image builds and the backend boots. Does NOT push to ACR and does NOT touch AKS — deploy happens only on merge via `ci_backend_deployment.yml`.

**Single-runner rule:** the image build, the running container, the pgvector service, and the tests all live in ONE job. Each GHA job runs on a fresh runner VM — a locally built image or running container from one job does not exist in another. Never split "build image" / "run backend" / "run tests" into separate jobs unless they interact over the network.

```
Jobs:
  run-quality:
    name: Run Quality Checks
    Steps: checkout → python 3.12 → install → ruff check src/ tests/ → ruff format --check → pyright src/

  build-and-test:
    name: Build Image and Run Tests
    Needs: run-quality
    Services: pgvector/pgvector:pg16 (localhost:5432)
    Steps:
      - Checkout
      - Docker build --tag sentinel-backend:test (local only, no push)
      - docker run -d --name sentinel-backend -p 8000:8000 sentinel-backend:test
        env: DATABASE_URL → local pgvector service, SENTINEL_FAKE_LLM=1 (stub provider calls)
      - Validate /health (poll with retry, max 30s) → validate /ready
      - pytest tests/test_tools/ tests/test_models/ -x          (unit)
      - pytest tests/test_agents/ tests/test_api/ -x            (integration)
      - docker stop + rm  (if: always())
```

**Key design:** No ACR, no AKS, no external LLM or Azure calls. `SENTINEL_FAKE_LLM=1` makes `/ready` skip the live-provider check and stubs LLM calls in tests, so PR CI is deterministic, free, and runs on forks. ~3-5 min. No smoke test either — that runs only on merge against the real deployment.

### 9.2 `ci_backend_deployment.yml` — Build, Push, Deploy to AKS (Post-Merge)

**Name:** `[sentinel] backend — deployment`

**Trigger:** `push` to `main` (paths: `src/**`, `tests/**`, `pyproject.toml`, `Dockerfile`, `azure/**`, `.github/actions/**`)

Runs after a PR merges to main. Builds the image, pushes it to ACR (immutable sha tag), scales the AKS node pool up if needed, deploys, validates the rollout, then runs the full test suite + smoke test against the LIVE deployment. Any failure after the deploy step triggers `kubectl rollout undo` — a broken merge never stays deployed. Ends by scaling back to zero (§8.5). Member of the `sentinel-backend` concurrency group.

```
Jobs:
  run-quality:
    name: Run Quality Checks
    Steps: checkout → python 3.12 → install → ruff check + format check → pyright src/

  build-and-push:
    name: Build and Push Image
    Needs: run-quality
    Steps:
      - Checkout
      - Azure login (OIDC) → az acr login
      - Docker build (tagged sha-{SHORT_SHA} only — no `latest`)
      - Docker push to ACR

  deploy-to-aks:
    name: Deploy to AKS
    Needs: build-and-push
    Outputs: backend-url   (from the backend-up action — no static URL variable)
    Steps:
      - Azure login (OIDC) → az aks get-credentials
      - Sync Key Vault secrets → K8s Secret `sentinel-secrets` (§8.2)
      - uses: ./.github/actions/backend-up   → node pool up, manifests applied,
        rollout waited, URL resolved from the cluster (output: backend-url)
      - kubectl set image deployment/sentinel-backend
          sentinel-backend=<ACR>/sentinel-backend:sha-{SHORT_SHA}
      - kubectl rollout status deployment/sentinel-backend --timeout=180s
      - Validate {backend-url}/health + /ready (DB connected, models reachable)

  run-tests:
    name: Run Tests Against Live Deployment
    Needs: deploy-to-aks
    Services: pgvector/pgvector:pg16   (isolated DB for unit/integration fixtures)
    Steps:
      - Checkout → install
      - pytest tests/test_tools/ tests/test_models/ -x           (unit)
      - pytest tests/test_agents/ tests/test_api/ -x             (integration)
      - Smoke test: POST canned incident payload to
        {needs.deploy-to-aks.outputs.backend-url}/webhooks/incident
        (Authorization: Bearer, aud api://sentinel-backend) → poll → assert
        incident stored, resolution or escalation produced, judge score present

  promote-or-rollback:
    name: Promote or Rollback
    Needs: run-tests
    if: always()
    Steps:
      - If run-tests succeeded:
          - Tag the validated image `stable` in ACR (bookmark for humans —
            AKS runs the immutable sha tag; nothing pulls `latest` at runtime)
          - ACR cleanup: keep the 3 most recent sha tags
      - If run-tests failed:
          - az aks get-credentials → kubectl rollout undo deployment/sentinel-backend
          - Teams notification: "backend deploy {SHORT_SHA} rolled back on main"
      - Always (unless vars.SENTINEL_KEEP_WARM == 'true'):
          - backend-down: replicas → 0, node pool → 0 (§8.5)
```

**Key design:** The deployed artifact is validated in place — the exact image now serving `ci_incident_response.yml` is the one the tests just exercised. There is no `latest` tag in the hot path: AKS pins the immutable sha tag, so a half-validated image can never be silently picked up by an incident run. The multi-job structure is legal here (unlike the PR gate) because jobs interact with ACR/AKS over the network, not with each other's local runner state.

**ACR image cleanup:**

```bash
# Keep latest 3 tags, delete the rest
TAGS=$(az acr repository show-tags --name sentinelacr --repository sentinel-backend \
  --orderby time_desc --output tsv)
KEEP=3
echo "$TAGS" | tail -n +$((KEEP+1)) | while read TAG; do
  az acr repository delete --name sentinelacr --image sentinel-backend:$TAG --yes
done
```

### 9.3 `ci_incident_response.yml` — The Real Pipeline

**Trigger:** `repository_dispatch` (event_type: `incident-alert`)

This is the GHA workflow that runs the incident response pipeline. Triggered by the Azure Function bridge when Datadog detects a failure.

**Key design:** The backend does the reasoning (triage, analysis, reflexion, decision). GHA does the execution (PR creation, notifications). The backend returns a detailed response including `resolution_type` (rollback or escalated), and GHA jobs branch on that.

```
Job Flow (scale-to-zero §8.5 — whole workflow in the `sentinel-backend` concurrency group):

  ensure-backend-up    ← backend-up action: node pool 0→1, replicas 0→1, wait /ready,
       │                 resolve URL from cluster → job output `backend-url`
       │                 cold start ~3-7 min + ~1-3 min LB (instant in KEEP_WARM session)
       │                 cannot become ready → Teams alert "incident NOT processed" + fail
       ├──────────────────────────────┬──────────────────────────────┐
       ▼                              ▼                              ▼
  fetch-service-info          fetch-pr-details              fetch-datadog-logs
       │                              │                              │
       └──────────────┬───────────────┴──────────────────────────────┘
                      ▼
               run-agent-pipeline      ← POST ${BACKEND_URL}/webhooks/incident + poll
                      │
                      │  response includes: resolution_type, root_cause, confidence, evidence
                      │  poll timeout (10 min) ⇒ treated as escalation, never a silent hang
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
     (gh pr create +           │
      psql UPDATE incidents    │
      SET pr_number, pr_url)   │
              │                │
              ▼                │
     notify-rollback           │
     (Teams: "Revert PR #N     │
      created — review         │
      and merge")              │
              │                │
              └───────┬────────┘
                      ▼
             teardown-backend         ← backend-down action (if: always();
                      │                 skipped when SENTINEL_KEEP_WARM=true)
                      ▼
                   summary            ← Datadog event + final Teams summary
```

```yaml
name: "[sentinel] incident response — full pipeline"

on:
  repository_dispatch:
    types: [incident-alert]

permissions:
  id-token: write      # OIDC login for Key Vault reads + AKS scaling
  contents: read

concurrency:
  group: sentinel-backend        # serialize with ci_backend_deployment (§8.5)
  cancel-in-progress: false

# No static backend URL — ensure-backend-up resolves it from the cluster (§8.5)
# and exposes it as the job output `backend-url` (non-secret, so outputs are legal).

# NOTE: There is no fetch-secrets job.
# - GHA refuses to pass masked secrets between jobs via outputs, so each job
#   that needs a secret fetches it itself (azure/login OIDC → az keyvault secret show).
# - Job outputs carry only non-secret context (metadata, log excerpts —
#   truncated to stay under the 1 MB job-output limit).

jobs:
  # ─── Job 0: Scale up the backend (scale-to-zero — §8.5) ──────────
  ensure-backend-up:
    runs-on: ubuntu-latest
    outputs:
      backend-url: ${{ steps.up.outputs.backend-url }}   # resolved from the cluster
    steps:
      - uses: actions/checkout@v4              # for ./.github/actions/backend-up
      - name: Azure Login (OIDC)
        uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
      - name: Backend up (node pool 0→1, replicas 0→1, wait /ready, resolve URL)
        id: up
        uses: ./.github/actions/backend-up     # ~3-7 min cold, no-op if warm
      - name: Alert Teams — backend failed to come up, incident NOT processed
        if: failure()
        run: |
          # az keyvault secret show teams-webhook-url
          # POST: "⚠ Incident alert received but backend could not be scaled up —
          #        incident NOT processed. Alert payload attached. Manual action needed."
          # The workflow fails visibly — an unprocessed incident is never silent.

  # ─── Jobs 1a/1b/1c: Parallel data gathering ──────────────────────
  fetch-service-info:
    needs: ensure-backend-up
    runs-on: ubuntu-latest
    outputs:
      service-metadata: ${{ steps.fetch.outputs.result }}
    steps:
      - name: Azure Login (OIDC)
        uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
      - name: Query service metadata from PostgreSQL
        id: fetch
        run: |
          SERVICE="${{ github.event.client_payload.tags.service }}"
          # PG_TOKEN=$(az account get-access-token --resource https://ossrdbms-aad.database.windows.net --query accessToken -o tsv)
          # psql (PGPASSWORD=$PG_TOKEN, user=sentinel-gha) query services table → JSON (non-secret)

  fetch-pr-details:
    needs: ensure-backend-up
    runs-on: ubuntu-latest
    outputs:
      pr-details: ${{ steps.fetch.outputs.result }}
    steps:
      - name: Get PR details from GitHub API
        id: fetch
        run: |
          VERSION="${{ github.event.client_payload.tags.version }}"
          PR_NUM=$(echo "$VERSION" | grep -oP 'pr-\K[0-9]+')
          # gh api repos/Keshav0375/Sentinel-deployment/pulls/$PR_NUM

  fetch-datadog-logs:
    needs: ensure-backend-up
    runs-on: ubuntu-latest
    outputs:
      recent-logs: ${{ steps.fetch.outputs.result }}
    steps:
      - name: Azure Login (OIDC)
        uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
      - name: Fetch recent error logs from Datadog
        id: fetch
        run: |
          SERVICE="${{ github.event.client_payload.tags.service }}"
          # DD_API_KEY=$(az keyvault secret show --vault-name sentinel-kv --name dd-api-key --query value -o tsv)
          # curl Datadog Logs API → last 30 min of errors (truncate: job outputs max 1 MB)

  # ─── Job 2: Run agent pipeline on the AKS backend ─────────────────
  run-agent-pipeline:
    needs: [ensure-backend-up, fetch-service-info, fetch-pr-details, fetch-datadog-logs]
    env:
      BACKEND_URL: ${{ needs.ensure-backend-up.outputs.backend-url }}
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
      - name: Azure Login (OIDC)
        uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
      - name: POST enriched payload to backend and poll for result
        id: run
        run: |
          # TOKEN=$(az account get-access-token --resource api://sentinel-backend --query accessToken -o tsv)
          # Merge: client_payload (incl. signal_type) + service metadata + PR details + logs
          # POST $BACKEND_URL/webhooks/incident (Authorization: Bearer $TOKEN) → incident_id
          #   signal_type=deploy_failure → backend rollback fast path; runtime_error → full pipeline (§3.1)
          # Poll GET $BACKEND_URL/incidents/$INCIDENT_ID every 15s, max 10 min
          # On timeout: set resolution-type=escalated, evidence="pipeline timeout" —
          # the workflow degrades to the escalation path, never a silent failure
          # Extract all fields from response into outputs

  # ─── Job 3a: ROLLBACK PATH — generate PR content ──────────────────
  generate-pr-content:
    needs: [ensure-backend-up, run-agent-pipeline]
    if: needs.run-agent-pipeline.outputs.resolution-type == 'rollback'
    env:
      BACKEND_URL: ${{ needs.ensure-backend-up.outputs.backend-url }}
    runs-on: ubuntu-latest
    outputs:
      pr-title: ${{ steps.generate.outputs.title }}
      pr-description: ${{ steps.generate.outputs.description }}
    steps:
      - name: Call PR content generation agent
        id: generate
        run: |
          # POST $BACKEND_URL/generate/pr-content (Authorization: Bearer) with:
          #   scenario context, root cause, target deploy, evidence
          # Returns: title, description (realistic developer-style text)

  # ─── Job 3b: ROLLBACK PATH — create revert PR ─────────────────────
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
          repository: Keshav0375/Sentinel-deployment
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

      - name: Record PR on incident row (creation record only — see §3.3)
        run: |
          # Azure login (OIDC) → PG_TOKEN=$(az account get-access-token \
          #   --resource https://ossrdbms-aad.database.windows.net --query accessToken -o tsv)
          # psql UPDATE incidents SET pr_number=$PR_NUM, pr_url=$PR_URL
          #   WHERE id='${{ needs.run-agent-pipeline.outputs.incident-id }}'
          # No lifecycle tracking — merge/close outcome lives on GitHub only

  # ─── Job 3c: ROLLBACK PATH — notify Teams ─────────────────────────
  notify-rollback:
    needs: [run-agent-pipeline, create-rollback-pr]
    runs-on: ubuntu-latest
    steps:
      - name: Notify Teams — revert PR created, review needed
        run: |
          # WEBHOOK=$(az keyvault secret show ... --name teams-webhook-url)  (after OIDC login)
          # POST to Teams webhook:
          # "Revert PR created — review and merge to deploy fix"
          # Include: PR URL, root cause, confidence, judge scores

  # ─── Job 3d: ESCALATION PATH — notify Teams directly ──────────────
  notify-escalation:
    needs: run-agent-pipeline
    if: needs.run-agent-pipeline.outputs.resolution-type == 'escalated'
    runs-on: ubuntu-latest
    steps:
      - name: Notify Teams — human intervention needed
        run: |
          # WEBHOOK=$(az keyvault secret show ... --name teams-webhook-url)  (after OIDC login)
          # POST to Teams webhook:
          # "Pipeline escalated — not confident enough to act"
          # Include: hypothesis, evidence, confidence score, what's missing

  # ─── Job 4: Teardown backend (always — skipped in KEEP_WARM) ──────
  teardown-backend:
    needs: [run-agent-pipeline, notify-rollback, notify-escalation]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Azure Login (OIDC)
        uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
      - name: Backend down (replicas → 0, node pool → 0)
        if: vars.SENTINEL_KEEP_WARM != 'true'
        uses: ./.github/actions/backend-down

  # ─── Job 5: Summary (always runs) ─────────────────────────────────
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
| Store incident to PostgreSQL | Yes (terminal state at pipeline completion) | No |
| Record `pr_number`/`pr_url` on incident | No | Yes (psql UPDATE right after PR creation) |
| Insert `deployments` rows | No | Yes — sentinel-deployment's `ci_app_deployment.yml` record-deployment stage |
| LangFuse tracing | Yes | No |

### 9.5 Workflow Summary

| File | Name | Trigger | Purpose | Jobs |
|------|------|---------|---------|------|
| `ci_validation.yml` | `[sentinel] PR — validation` | PR to main | Fast gate — lint, typecheck, Docker build (local), run backend + tests in one job | run-quality → build-and-test |
| `ci_backend_deployment.yml` | `[sentinel] backend — deployment` | Push to main | Post-merge deploy — build+push to ACR, scale up AKS, deploy, validate rollout, all tests against live deployment, smoke test, promote or rollback, scale to zero | run-quality → build-and-push → deploy-to-aks → run-tests → promote-or-rollback (+ scale to zero) |
| `ci_incident_response.yml` | `[sentinel] incident — response pipeline` | repository_dispatch | **Real incident pipeline** — scales the backend up, runs, scales it down (§8.5) | ensure-backend-up → parallel fetch → run-agent-pipeline → branch (rollback / escalate) → teardown-backend → summary |
| `ci_backend_scale.yml` | `[sentinel] backend — scale` | workflow_dispatch (up/down) + nightly cron (down) | Manual toggle + safety net for scale-to-zero (§8.5) | scale |

---

## 10. Terraform Justification

### Why Terraform (Not Azure CLI Scripts, Not Bicep, Not Manual)

| Alternative | Why Not |
|---|---|
| **Manual (Azure Portal)** | Can't reproduce. Can't version. Can't review. One click and state drifts. |
| **Azure CLI scripts** | Imperative — no state tracking. Can't diff what exists vs. what's desired. Idempotency is manual. |
| **Bicep** | Azure-only. Sentinel may expand to GCP/AWS later. Terraform is provider-agnostic. Also: Terraform experience is more transferable for interviews. |

### What Terraform Provisions (7 Modules)

| Module | Resources Created | Why It Exists |
|--------|------------------|---------------|
| `aks/` | AKS cluster (free control plane) + 1× B2ats_v2 node pool + AcrPull role for kubelet | Hosts the sentinel-backend single-replica Deployment |
| `acr/` | Container Registry | Stores Docker images (pulled by AKS and CI) |
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
| `pyjwt[crypto]` | Validate inbound Entra bearer JWTs against JWKS (§3.6) | None (new — replaces `X-Sentinel-Token`) |
| `azure-identity` | `WorkloadIdentityCredential` / `DefaultAzureCredential` — pod fetches Entra tokens for Key Vault + PostgreSQL (§8.2) | None (new) |
| `azure-keyvault-secrets` | Read LLM/LangFuse keys from Key Vault at runtime via workload identity | None (new — replaces K8s Secret sync) |

---

## 12. Prerequisites & Setup Checklist

### Azure (provisioned by sentinel-infra)
- [ ] AKS cluster (1× B2ats_v2 node) with AcrPull on ACR — `az aks get-credentials` works
- [ ] AKS **workload identity** enabled (OIDC issuer) + backend UAMI + federated credential (§sentinel-infra §3.7); ServiceAccount `sentinel-backend` annotated with the UAMI client-id
- [ ] ACR with admin access or service principal
- [ ] PostgreSQL B1MS with pgvector — **Entra-only auth**; the GHA SP + backend UAMI mapped to DB roles (no `db-password`)
- [ ] Backend registered as an Entra app (`api://sentinel-backend`, `Incident.Write` role) for inbound auth (§3.6 / sentinel-infra §4.4)
- [ ] Key Vault with secrets: anthropic-api-key, openai-api-key, dd-api-key, teams-webhook-url, langfuse-secret-key, langfuse-public-key (**no** db-password, **no** sentinel-api-token)

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
- [ ] `kubectl` + `az` CLI (for inspecting the AKS deployment)

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
| `src/sentinel/eval/runner.py` | Iterates synthetic scenarios from `data/` → **deploys the 30 sentinel-deployment scenario branches** and scores agent output against each branch's ground-truth label in `scenarios/branches.yaml` (case, expected `signal_type`, expected resolution). The branches ARE the eval dataset; results push to LangFuse. |
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
| `src/sentinel/api/auth.py` | **Entra bearer validation** — `require_incident_write` FastAPI dependency (JWKS, aud/iss/exp/role) (§3.6) |
| `src/sentinel/infra/azure_identity.py` | Workload-identity helpers — Key Vault reads + PostgreSQL Entra token acquisition (§8.2) |
| `alembic/` | Migration directory with `alembic.ini`, `env.py`, versions/ |
| `alembic/versions/001_initial_schema.py` | Creates 3 tables + pgvector extension |
| `alembic/versions/002_seed_services.py` | Inserts initial service data (replaces `data/seed.py`) |
| `azure/k8s/deployment.yaml` | Single-replica backend Deployment (workload-identity SA + label, probes, resources, `envFrom` non-secret ConfigMap) |
| `azure/k8s/serviceaccount.yaml` | Workload-identity ServiceAccount (annotated with backend UAMI client-id) |
| `azure/k8s/service.yaml` | LoadBalancer Service — created/deleted per run (dynamic URL, §8.5) |
| `.github/actions/backend-up/` | Composite: scale up + apply manifests + wait ready + output `backend-url` |
| `.github/actions/backend-down/` | Composite: delete Service + scale to zero |
| `.github/actions/get-kv-secrets/` | Composite: fetch + mask Key Vault secrets |
| `.github/actions/get-db-token/` | Composite: mint + mask an Entra PostgreSQL token (replaces db-password fetch) |
| `.github/actions/get-backend-token/` | Composite: mint + mask an `api://sentinel-backend` token |
| `.github/actions/notify-teams/` | Composite: Teams webhook notification |
| `.github/actions/psql-exec/` | Composite: run SQL against sentinel PostgreSQL (token auth) |
| `.github/workflows/ci_validation.yml` | Fast PR gate — quality + single-job build/run/test |
| `.github/workflows/ci_backend_deployment.yml` | Post-merge — build+push to ACR, deploy to AKS, tests, promote/rollback, scale to zero |
| `.github/workflows/ci_incident_response.yml` | Real incident pipeline workflow |
| `.github/workflows/ci_backend_scale.yml` | Manual up/down toggle + nightly auto-down |

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
10. **Add K8s manifests** — `azure/k8s/deployment.yaml`, `azure/k8s/service.yaml`
11. **Add composite actions** — `backend-up`, `backend-down`, `get-kv-secrets`, `notify-teams`, `psql-exec` in `.github/actions/`
12. **Update CI** — add `ci_validation.yml` (PR gate), `ci_backend_deployment.yml` (post-merge deploy), `ci_incident_response.yml` (incident pipeline), `ci_backend_scale.yml` (scale toggle + safety net)

Step 8 comes late intentionally — keep Phase 1 working until Phase 2 tools are proven.

### 13.6 GHA Workflow Rules (Learned the Hard Way, on Paper)

Two GitHub Actions facts every workflow in this repo must respect:

1. **Each job = a fresh runner VM.** Local state (built images, running containers,
   files) does not survive across jobs. Jobs may only depend on each other through
   (a) job outputs, (b) artifacts, or (c) shared external systems (ACR, AKS, PostgreSQL).
2. **Secrets cannot cross job boundaries via outputs.** GHA masks and drops job
   outputs that contain secrets. Each job fetches its own secrets (OIDC → Key Vault).

---

## 14. Cost Breakdown

| Resource | Monthly Cost | Covered By |
|----------|-------------|------------|
| AKS control plane | Free | Always free |
| AKS node (1× B2ats_v2) | Free | Scale-to-zero (§8.5): ~20-80 hrs/mo consumed of the free 750 (12-month, expires 05/2027) — the rest stays available for other projects |
| LoadBalancer public IP | ~$0 | Released at teardown (Service deleted, URL resolved fresh per run) — bills only for scaled-up hours, cents/month |
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
| **Total** | **~$5-12/month** | **LLM costs only — infrastructure is $0 at idle.** |

**After 12 months (05/2027):** PostgreSQL and ACR start billing; the AKS node bills only for scaled-up hours (scale-to-zero) — roughly $20-35/month total. The demo window sits inside the free period; `terraform destroy` tears everything down when the project wraps.
