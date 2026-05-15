## What Is Sentinel?

Sentinel is an **autonomous DevOps incident response agent**. Think of it as an AI on-call engineer that receives an alert (e.g., "error rate spiked on api-gateway"), investigates the problem across multiple data sources, figures out the root cause, drafts a fix, and writes a Slack summary — all in seconds instead of the 20-40 minutes a human would take.

## The Real-World Problem It Solves

When a production service breaks at 3 AM:

1. An **on-call engineer** gets paged
2. They manually check **dashboards** to understand the alert
3. They dig through **logs** to find error patterns
4. They check **recent deploys** to see if someone shipped something bad
5. They decide whether to **rollback** or **hotfix**
6. They write a **Slack update** to keep the team informed

Sentinel automates steps 2-6 using a team of AI agents that each specialize in one part of that workflow. The human stays in the loop for the dangerous step — actually executing the fix.

## How It Works (The Pipeline)

```
Alert fires
  └→ Orchestrator receives it (the "manager" agent)
       ├→ Triage Agent:     "This is P1, affects api-gateway"
       ├→ Log Analyst:      "I see NullPointerException in AuthMiddleware"
       ├→ Deploy Correlator: "deploy-abc123 went out 15 min ago, 5 files changed"
       ├→ Remediation Agent: "I'm drafting a rollback PR" → ⚠️ HITL GATE → human approves
       └→ Comms Agent:      "Here's the Slack summary for #incidents"
  └→ Incident resolved, trajectory saved, eval scores it
```

**5 specialist agents**, each with their own system prompt, tools, and LLM model. The Orchestrator hands off to each one in sequence. Each agent can only use the tools assigned to it — the Log Analyst can't draft a rollback, the Triage Agent can't write Slack summaries.

**The critical safety rule:** No agent can execute a destructive action (rollback, restart, etc.) without going through the `request_human_approval` HITL gate. There is no "execute rollback" tool — only "draft rollback" + "ask a human."

## What Data Does It Use?

For the MVP, everything is **synthetic but realistic**. You have 10 pre-built scenarios across 5 failure classes:

| Failure Class | Example |
|---|---|
| Bad Deploy | Missing config key causes NullPointerException |
| DB Pool Exhaustion | Connection leak from unclosed cursors |
| Downstream Outage | Third-party payment API goes down |
| Memory Leak | OOM from unbounded cache |
| Config Regression | Feature flag rollout breaks subset of users |

Each scenario file (`data/scenarios/bad_deploy_01.json`) contains: the alert payload, realistic log entries (with red herrings mixed in), deploy history, and the known ground truth so the eval system can score the agent's performance.

## How To Run It

### Option 1: Single scenario from CLI

```bash
# From project root, with venv active
python scripts/run_scenario.py bad_deploy_01
```

What happens:
1. Loads the scenario JSON
2. Seeds the SQLite database with service metadata and runbooks
3. Loads the `all-MiniLM-L6-v2` embedding model locally
4. Connects to Groq API (your LLM provider — fast, free-tier Llama models)
5. Builds all 6 agents with the scenario data injected into the log/deploy tools
6. Runs the full pipeline — you'll see agent handoffs in your terminal
7. When the Remediation Agent fires, **you get a terminal prompt**: type `approve` or `reject`
8. Prints the full timeline + ground truth comparison

### Option 2: Web dashboard

```bash
poetry install

poetry run sentinel serve
# Open http://localhost:8000

Other commands that now work:
  poetry run sentinel scenarios          # list all 10 scenarios
  poetry run sentinel scenario bad_deploy_01  # run one interactively in terminal
  poetry run sentinel serve --port 3000  # custom port
  poetry run pytest -xvs                 # run tests
```

What you see:
- **Dark GitHub-themed dashboard** with a "Fire Scenario" dropdown at the top
- Select a scenario → click "Fire" → watch it in real-time via SSE (Server-Sent Events)
- **Agent pipeline cards** light up one by one as each specialist starts/finishes
- **Tool calls** appear as sub-items under each agent card
- When the HITL gate fires, an **approve/reject overlay** appears — click a button instead of typing
- **Bottom metrics bar**: tool call count, elapsed time, memory hits
- Link to **Eval Results page** (`/eval`) showing per-scenario scores

### Option 3: Run the full eval suite

```bash
python scripts/run_eval.py
```

Fires all 10 scenarios, auto-approves the HITL gates, captures every trajectory, then runs an LLM-as-judge that scores each incident across 6 dimensions (0-5 each):

| Dimension | What It Measures |
|---|---|
| Triage Accuracy | Right service? Right severity? |
| Root Cause Correctness | Does the hypothesis match ground truth? |
| Tool Efficiency | Logical tool order? No wasted calls? |
| MTTR | Time from alert to fix proposal |
| Remediation Safety | Did it gate destructive actions properly? |
| Comms Quality | Clear, complete Slack summary? |

Outputs: `reports/eval_report.json` + `reports/eval_report.md`, viewable at `/eval` on the dashboard.

## What You Need in `.env`

```
GROQ_API_KEY=gsk_...        # Primary LLM (Llama 3.3 via Groq, free tier works)
GEMINI_API_KEY=...           # Optional backup
```

No OpenAI key needed — Groq is OpenAI-compatible. The embedding model (`all-MiniLM-L6-v2`) runs locally, zero cost.

## What the Logs Look Like

When the pipeline runs, `structlog` emits JSON logs with `incident_id` correlation:

```
{"event": "pipeline_start", "incident_id": "smoke-a1b2c3d4", "service": "api-gateway"}
{"event": "tool_called", "incident_id": "smoke-a1b2c3d4", "agent": "triage", "tool": "get_service_metadata"}
{"event": "handoff", "incident_id": "smoke-a1b2c3d4", "from": "triage", "to": "log_analyst"}
{"event": "hitl_requested", "incident_id": "smoke-a1b2c3d4", "action": "rollback deploy-abc123", "risk": "high"}
{"event": "pipeline_complete", "incident_id": "smoke-a1b2c3d4"}
```

Every tool call, handoff, and HITL decision gets saved to a trajectory JSON file in `reports/trajectories/`. That's what the eval system reads.

## The Memory System

- **Short-term memory**: In-process Python dict — the current incident's timeline, tool results, agent outputs. Gone when the process dies.
- **Episodic memory**: SQLite — every resolved incident stored with symptom embeddings. On the next incident, the Triage Agent queries "have I seen something like this before?" using cosine similarity. If a past incident with the same pattern was caused by a bad deploy, the agent skips straight to checking deploys.
- **Semantic memory**: SQLite — service ownership, dependency graphs, runbooks. "Who owns api-gateway? What services does it depend on? What's the runbook for a bad deploy on this service?"

## Project Status

~92% complete. Everything works end-to-end. Remaining: demo script with rich terminal output (8.2), GitHub Actions CI (8.3), and final polish/edge cases (8.4).
