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

from agents import Runner, add_trace_processor, handoff  # noqa: E402
from agents import trace as agent_trace  # noqa: E402

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
from sentinel.providers import apply_sdk_defaults, get_capabilities  # noqa: E402

_SEP = "=" * 62


def _print_header(title: str) -> None:
    """Print a section header to stdout."""
    print(f"\n{_SEP}")
    print(f"  {title}")
    print(_SEP)


def _safe_print(text: str) -> None:
    """Print text with fallback for Windows cp1252 encoding."""
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.flush()


def _print_timeline(stm: ShortTermMemory, incident_id: str) -> None:
    """Print the incident timeline from ShortTermMemory."""
    try:
        ctx = stm.get_context(incident_id)
    except KeyError:
        print("  (incident was cleared)")
        return

    _safe_print(f"  status:  {ctx['status']}")
    _safe_print(f"  service: {ctx['alert'].service}")
    print()

    timeline = ctx["timeline"]
    if not timeline:
        print("  (no timeline entries recorded)")
        return

    for i, entry in enumerate(timeline, 1):
        ts = entry.timestamp.strftime("%H:%M:%S")
        _safe_print(f"  [{i:02d}] {ts}  [{entry.agent_name}] {entry.action}")
        summary = entry.result_summary
        if len(summary) > 120:
            summary = summary[:117] + "..."
        _safe_print(f"        -> {summary}")


async def _auto_approval(display: str) -> tuple[str, str | None]:
    """Auto-approve HITL gate for non-interactive runs."""
    import sys  # noqa: PLC0415

    sys.stdout.buffer.write(display.encode("utf-8", errors="replace"))
    sys.stdout.buffer.write(b"\n>>> AUTO-APPROVED (--auto-approve flag)\n")
    sys.stdout.buffer.flush()
    return ("approve", "auto-approved for demo/test run")


async def run_scenario(
    scenario_id: str,
    *,
    db_path: Path | None = None,
    trajectories_dir: Path | None = None,
    auto_approve: bool = False,
) -> None:
    """Build the full pipeline, run it against a scenario, print the trajectory.

    This is the primary function called by the CLI. It can also be imported
    and called from tests with a mock db_path to avoid seeding a real database.

    Args:
        scenario_id: Scenario to run (e.g. ``"bad_deploy_01"``).
        db_path: Override the SQLite path from Settings.
        trajectories_dir: Override the trajectory output directory.
        auto_approve: If True, auto-approve all HITL gates (for CI/demo).
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

    # ── SDK defaults + provider layer ─────────────────────────────────────────
    apply_sdk_defaults(settings)
    print(f"  [OK] provider: {settings.sentinel_analysis_model.split('/')[0]}")

    # ── Build agents with scenario injected ───────────────────────────────────
    # Inject the scenario into log and deploy tools so they return
    # synthetic data specific to this scenario rather than error messages.
    triage_agent = build_triage_agent(
        semantic_memory,
        episodic_memory,
        model_string=settings.sentinel_triage_model,
        settings=settings,
    )
    log_analyst_agent = build_log_analyst_agent(
        semantic_memory,
        scenario,  # synthetic logs from this scenario
        model_string=settings.sentinel_analysis_model,
        settings=settings,
    )
    deploy_correlator_agent = build_deploy_correlator_agent(
        scenario,  # synthetic deploys from this scenario
        model_string=settings.sentinel_triage_model,
        settings=settings,
    )
    approval_fn = _auto_approval if auto_approve else None
    remediation_agent = build_remediation_agent(
        model_string=settings.sentinel_analysis_model,
        settings=settings,
        approval_fn=approval_fn,
    )
    comms_agent = build_comms_agent(
        model_string=settings.sentinel_triage_model,
        settings=settings,
    )
    orchestrator = build_orchestrator_agent(
        triage_agent,
        log_analyst_agent,
        deploy_correlator_agent,
        remediation_agent,
        comms_agent,
        model_string=settings.sentinel_analysis_model,
        settings=settings,
    )

    # Wire handoff-back so specialists return control to the orchestrator.
    caps = get_capabilities(settings.sentinel_analysis_model)
    back = handoff(orchestrator)
    back.strict_json_schema = caps.strict_schemas
    for specialist in [
        triage_agent,
        log_analyst_agent,
        deploy_correlator_agent,
        remediation_agent,
        comms_agent,
    ]:
        specialist.handoffs = [back]

    print("  [OK] agents built (scenario injected into log/deploy tools)")

    # ── Incident setup ─────────────────────────────────────────────────────────
    incident_id = f"smoke-{alert.alert_id[:8]}"
    tracer = SentinelTracer(short_term_memory, trajectories_dir=resolved_traj)
    add_trace_processor(tracer)
    short_term_memory.create(incident_id, alert)
    print(f"  [OK] incident: {incident_id}")

    # ── Run pipeline ───────────────────────────────────────────────────────────
    _print_header(f"Running pipeline  [{incident_id}]")
    if auto_approve:
        print("  HITL gates will be AUTO-APPROVED (--auto-approve flag).\n")
    else:
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
                max_turns=settings.sentinel_max_tool_calls + 5,
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
        "  python scripts/run_scenario.py <scenario_id> [--auto-approve]\n"
        "  python scripts/run_scenario.py --list\n\n"
        "Options:\n"
        "  --auto-approve   Auto-approve all HITL gates (for CI/demo)\n\n"
        "Examples:\n"
        "  python scripts/run_scenario.py bad_deploy_01\n"
        "  python scripts/run_scenario.py bad_deploy_01 --auto-approve\n"
        "  python scripts/run_scenario.py db_pool_01\n"
    )


def main() -> None:
    """CLI entry point — parse args and run the scenario."""
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        _usage()
        sys.exit(0 if "--help" in args else 1)

    if args[0] == "--list":
        scenarios = list_scenarios()
        if scenarios:
            print("Available scenarios:")
            for s in scenarios:
                print(f"  {s}")
        else:
            print("No scenarios found.")
        return

    scenario_id = args[0]
    auto_approve = "--auto-approve" in args
    try:
        asyncio.run(run_scenario(scenario_id, auto_approve=auto_approve))
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
