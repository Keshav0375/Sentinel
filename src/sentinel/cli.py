"""Sentinel CLI — one-command startup for the incident response agent.

Usage::

    sentinel serve          # launch the app on :8000
    sentinel serve --port 3000
"""

from __future__ import annotations

import sys


def _serve(port: int = 8000, host: str = "0.0.0.0") -> None:
    """Launch the FastAPI app (dashboard + API)."""
    import uvicorn

    print("=" * 50)
    print("  Sentinel — Autonomous Incident Response Agent")
    print("=" * 50)
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


def main() -> None:
    """CLI entry point — registered as `sentinel` command via pyproject.toml."""
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(
            "Usage:\n"
            "  sentinel serve [--port PORT]    Launch the app (dashboard + API)\n"
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

    else:
        print(f"Unknown command: {cmd}")
        print("Run `sentinel --help` for usage.")
        sys.exit(1)
