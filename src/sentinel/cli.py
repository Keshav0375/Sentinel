"""Sentinel CLI — one-command startup for the incident response agent.

Usage::

    sentinel serve          # seed DB + launch dashboard on :8000
    sentinel serve --port 3000
    sentinel scenario bad_deploy_01   # run a single scenario interactively
    sentinel scenarios      # list available scenarios
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


def _seed_database() -> None:
    """Seed the SQLite database with service map and runbooks."""
    from sentinel.config import get_settings
    from sentinel.infra.db import create_tables

    settings = get_settings()
    db_path = settings.sentinel_db_path

    async def _seed() -> None:
        await create_tables(db_path)
        try:
            from data.seed import seed

            counts = await seed(db_path, verbose=False)
            print(f"  [OK] Database seeded — {counts[0]} services, {counts[1]} runbooks")
        except ImportError:
            print("  [WARN] data.seed not found — skipping seed")

    asyncio.run(_seed())


def _serve(port: int = 8000, host: str = "0.0.0.0") -> None:
    """Seed the database and launch the FastAPI dashboard."""
    import uvicorn

    print("=" * 50)
    print("  Sentinel — Autonomous Incident Response Agent")
    print("=" * 50)
    print()
    print("  Seeding database...")
    _seed_database()
    print()
    print(f"  Starting dashboard on http://localhost:{port}")
    print("  Press Ctrl+C to stop")
    print()

    uvicorn.run(
        "sentinel.main:app",
        host=host,
        port=port,
        reload=True,
    )


def _run_scenario(scenario_id: str) -> None:
    """Run a single scenario end-to-end with interactive HITL."""
    _seed_database()

    # Import run_scenario by adding scripts/ to sys.path at runtime,
    # since scripts/ is not a Python package.
    scripts_dir = str(Path(__file__).resolve().parent.parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    from run_scenario import run_scenario  # type: ignore[import-not-found]

    asyncio.run(run_scenario(scenario_id))  # pyright: ignore[reportUnknownArgumentType]


def _list_scenarios() -> None:
    """Print all available scenario IDs."""
    from sentinel.generator.alert_gen import list_scenarios

    scenarios = list_scenarios()
    if scenarios:
        print("Available scenarios:")
        for s in scenarios:
            print(f"  {s}")
    else:
        print("No scenarios found.")


def main() -> None:
    """CLI entry point — registered as `sentinel` command via pyproject.toml."""
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(
            "Usage:\n"
            "  sentinel serve [--port PORT]    Seed DB + launch dashboard\n"
            "  sentinel scenario <id>          Run a single scenario interactively\n"
            "  sentinel scenarios              List available scenarios\n"
            "  sentinel --help                 Show this help\n"
        )
        return

    cmd = args[0]

    if cmd == "serve":
        port = 8000
        if "--port" in args:
            idx = args.index("--port")
            if idx + 1 < len(args):
                port = int(args[idx + 1])
        _serve(port=port)

    elif cmd == "scenario":
        if len(args) < 2:
            print("Error: missing scenario ID. Run `sentinel scenarios` to see options.")
            sys.exit(1)
        _run_scenario(args[1])

    elif cmd == "scenarios":
        _list_scenarios()

    else:
        print(f"Unknown command: {cmd}")
        print("Run `sentinel --help` for usage.")
        sys.exit(1)
