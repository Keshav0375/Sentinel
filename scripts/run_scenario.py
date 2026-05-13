"""Fire a single scenario end-to-end.

Usage::

    python scripts/run_scenario.py bad_deploy_01
    python scripts/run_scenario.py --list
    python scripts/run_scenario.py --help

Run from the project root so that ``src/`` and ``data/`` are importable.
The script builds the full agent pipeline, injects the scenario into the
log/deploy tools so they return synthetic data, then runs the orchestrator
and prints the full timeline. HITL approval prompts appear inline.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

# Ensure src/ is on sys.path when running the script directly.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agents import Runner, add_trace_processor, set_default_openai_client  # noqa: E402
from agents import trace as agent_trace  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from sentinel.agents.comms import build_comms_agent  # noqa: E402
from sentinel.agents.deploy_correlator import build_deploy_correlator_agent  # noqa: E402
from sentinel.agents.log_analyst import build_log_analyst_agent  # noqa: E402
from sentinel.agents.orchestrator import build_orchestrator_agent  # noqa: E402
from sentinel.agents.remediation import build_remediation_agent  # noqa: E402
from sentinel.agents.triage import build_triage_agent  # noqa: E402
from sentinel.config import get_settings  # noqa: E402
from sentinel.generator.alert_gen import generate_alert, list_scenarios, load_scenario  # noqa: E402
from sentinel.infra.db import create_tables  # noqa: E402
from sentinel.infra.logging import configure_logging  # noqa: E402
from sentinel.infra.tracing import SentinelTracer  # noqa: E402
from sentinel.memory.embeddings import EmbeddingClient  # noqa: E402
from sentinel.memory.episodic import EpisodicMemory  # noqa: E402
from sentinel.memory.semantic import SemanticMemory  # noqa: E402
from sentinel.memory.short_term import ShortTermMemory  # noqa: E402
from sentinel.models.incident import IncidentStatus  # noqa: E402

_SEP = "=" * 62


def _print_header(title: str) -> None:
    """Print a section header to stdout."""
    print(f"\n{_SEP}")
    print(f"  {title}")
    print(_SEP)


def _print_timeline(stm: ShortTermMemory, incident_id: str) -> None:
    """Print the incident timeline from ShortTermMemory."""
    try:
        ctx = stm.get_context(incident_id)
    except KeyError:
        print("  (incident was cleared)")
        return

    print(f"  status:  {ctx['status']}")
    print(f"  service: {ctx['alert'].service}")
    print()

    timeline = ctx["timeline"]
    if not timeline:
        print("  (no timeline entries recorded)")
        return

    for i, entry in enumerate(timeline, 1):
        ts = entry.timestamp.strftime("%H:%M:%S")
        print(f"  [{i:02d}] {ts}  [{entry.agent_name}] {entry.action}")
        summary = entry.result_summary
        if len(summary) > 120:
            summary = summary[:117] + "..."
        print(f"        -> {summary}")


async def run_scenario(
    scenario_id: str,
    *,
    db_path: Path | None = None,
    trajectories_dir: Path | None = None,
) -> None:
    """Build the full pipeline, run it against a scenario, print the trajectory.

    This is the primary function called by the CLI. It can also be imported
    and called from tests with a mock db_path to avoid seeding a real database.

    Args:
        scenario_id: Scenario to run (e.g. ``"bad_deploy_01"``).
        db_path: Override the SQLite path from Settings.
        trajectories_dir: Override the trajectory output directory.
    """
    settings = get_settings()
    # Keep INFO logs quiet during the demo — HITL prompts go to stdout directly.
    configure_logging("WARNING")

    resolved_db = db_path or settings.sentinel_db_path
    resolved_traj = trajectories_dir or Path("reports/trajectories")

    # ── Load scenario ──────────────────────────────────────────────────────────
    _print_header(f"Scenario: {scenario_id}")
    scenario = load_scenario(scenario_id)  # raises FileNotFoundError if missing
    alert = generate_alert(scenario)
    print(f"  scenario_id:   {scenario.scenario_id}")
    print(f"  failure_class: {scenario.failure_class}")
    print(f"  service:       {scenario.alert.service}")
    print(f"  alert_id:      {alert.alert_id}")
    print(f"  description:   {scenario.description}")

    # ── Database + seed ────────────────────────────────────────────────────────
    _print_header("Setting up")
    await create_tables(resolved_db)
    try:
        from data.seed import seed  # noqa: PLC0415

        await seed(resolved_db, verbose=False)
    except ImportError:
        print("  [WARN] data.seed not importable — skipping seed (run from project root)")
    print(f"  [OK] database: {resolved_db}")

    # ── Memory clients ─────────────────────────────────────────────────────────
    embedding_client = EmbeddingClient.from_model_name(settings.sentinel_embedding_model)
    semantic_memory = SemanticMemory(resolved_db)
    episodic_memory = EpisodicMemory(resolved_db, embedding_client)
    short_term_memory = ShortTermMemory()
    print(f"  [OK] embedding model: {settings.sentinel_embedding_model}")

    # ── Groq client ────────────────────────────────────────────────────────────
    groq_client = AsyncOpenAI(api_key=settings.groq_api_key, base_url=settings.groq_base_url)
    set_default_openai_client(groq_client)
    print(f"  [OK] LLM: {settings.groq_base_url}")

    # ── Build agents with scenario injected ───────────────────────────────────
    # Inject the scenario into log and deploy tools so they return
    # synthetic data specific to this scenario rather than error messages.
    triage_agent = build_triage_agent(
        semantic_memory,
        episodic_memory,
        groq_client=groq_client,
        model_name=settings.sentinel_triage_model,
    )
    log_analyst_agent = build_log_analyst_agent(
        semantic_memory,
        scenario,  # synthetic logs from this scenario
        groq_client=groq_client,
        model_name=settings.sentinel_analysis_model,
    )
    deploy_correlator_agent = build_deploy_correlator_agent(
        scenario,  # synthetic deploys from this scenario
        groq_client=groq_client,
        model_name=settings.sentinel_triage_model,
    )
    remediation_agent = build_remediation_agent(
        groq_client=groq_client,
        model_name=settings.sentinel_analysis_model,
    )
    comms_agent = build_comms_agent(
        groq_client=groq_client,
        model_name=settings.sentinel_triage_model,
    )
    orchestrator = build_orchestrator_agent(
        triage_agent,
        log_analyst_agent,
        deploy_correlator_agent,
        remediation_agent,
        comms_agent,
        groq_client=groq_client,
        model_name=settings.sentinel_analysis_model,
    )
    print("  [OK] agents built (scenario injected into log/deploy tools)")

    # ── Incident setup ─────────────────────────────────────────────────────────
    incident_id = f"smoke-{alert.alert_id[:8]}"
    tracer = SentinelTracer(short_term_memory, trajectories_dir=resolved_traj)
    add_trace_processor(tracer)
    short_term_memory.create(incident_id, alert)
    print(f"  [OK] incident: {incident_id}")

    # ── Run pipeline ───────────────────────────────────────────────────────────
    _print_header(f"Running pipeline  [{incident_id}]")
    print("  HITL approval prompts will appear when the Remediation Agent runs.")
    print("  Type 'approve' or 'reject' and press Enter.\n")

    input_text = (
        f"New incident ID: {incident_id}\n"
        f"Alert payload: {alert.model_dump_json()}"
    )
    start = datetime.now(UTC)

    try:
        with agent_trace("incident_pipeline", metadata={"incident_id": incident_id}):
            await Runner.run(
                orchestrator,
                input_text,
                max_turns=settings.sentinel_max_tool_calls,
            )
        elapsed = (datetime.now(UTC) - start).total_seconds()
        print(f"\n  [OK] pipeline completed in {elapsed:.1f}s")
        try:
            short_term_memory.update_context(incident_id, status=IncidentStatus.RESOLVED)
        except KeyError:
            pass
    except Exception as exc:
        elapsed = (datetime.now(UTC) - start).total_seconds()
        print(f"\n  [ERROR] pipeline failed after {elapsed:.1f}s: {exc}")
        try:
            short_term_memory.update_context(incident_id, status=IncidentStatus.ESCALATED)
        except KeyError:
            pass

    # ── Print timeline ─────────────────────────────────────────────────────────
    _print_header("Incident Timeline")
    _print_timeline(short_term_memory, incident_id)

    # ── Print ground truth ─────────────────────────────────────────────────────
    _print_header("Ground Truth (for eval comparison)")
    gt = scenario.ground_truth
    print(f"  expected severity:   {gt.severity}")
    print(f"  affected service:    {gt.affected_service}")
    print(f"  root cause:          {gt.root_cause_summary}")
    print(f"  recommended action:  {gt.recommended_action}")
    if gt.deploy_id:
        print(f"  culprit deploy:      {gt.deploy_id}")

    traj_path = resolved_traj / f"{incident_id}.json"
    if traj_path.exists():
        print(f"\n  Trajectory: {traj_path}")

    print()


def _usage() -> None:
    print(
        "Usage:\n"
        "  python scripts/run_scenario.py <scenario_id>\n"
        "  python scripts/run_scenario.py --list\n\n"
        "Examples:\n"
        "  python scripts/run_scenario.py bad_deploy_01\n"
        "  python scripts/run_scenario.py db_pool_01\n"
        "  python scripts/run_scenario.py downstream_outage_01\n"
    )


def main() -> None:
    """CLI entry point — parse args and run the scenario."""
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        _usage()
        sys.exit(0 if "--help" in sys.argv[1:] else 1)

    if sys.argv[1] == "--list":
        scenarios = list_scenarios()
        if scenarios:
            print("Available scenarios:")
            for s in scenarios:
                print(f"  {s}")
        else:
            print("No scenarios found.")
        return

    scenario_id = sys.argv[1]
    try:
        asyncio.run(run_scenario(scenario_id))
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print("\nAvailable scenarios:", file=sys.stderr)
        for s in list_scenarios():
            print(f"  {s}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
