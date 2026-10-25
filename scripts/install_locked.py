#!/usr/bin/env python3
"""Install exactly the poetry.lock set (main + dev groups) into the running Python.

CI runs this so tests, pyright and the audit all vet the locked versions — a plain
`pip install .` resolves pyproject ranges afresh and drifts from the lock. Run it the
same way locally to reproduce CI:

    python3.12 -m venv /tmp/ci-venv
    /tmp/ci-venv/bin/python scripts/install_locked.py

Needs `poetry` with poetry-plugin-export on PATH. Every package except torch installs
from the export with its lock hashes and no dependency resolution. torch comes from the
CPU-only index at the locked version: the PyPI wheel drags in the CUDA stack (cuda-*,
nvidia-*, triton), which is dropped from the set — the CPU wheel needs none of it.
`pip check` then proves the installed set is complete and consistent.

`--main-only` (the container image, Dockerfile): the main group only, and the project
itself is not installed. The image runs from `src/` on PYTHONPATH, and `src/` carries the
package data (prompts, dashboard, the scenario catalog).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
# torch plus the CUDA runtime only the PyPI torch wheel needs.
CUDA_STACK = re.compile(r"^(torch|triton|cuda-[a-z0-9-]+|nvidia-[a-z0-9-]+)$")
REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")


def run(*argv: str) -> None:
    print("+", " ".join(argv), flush=True)
    subprocess.run(argv, check=True)


def split_requirements(exported: str) -> tuple[list[str], str]:
    """Split an exported requirements file into (hashed entries, locked torch version).

    An entry spans its `\\`-continued `--hash` lines; it starts on an unindented line.
    """
    entries: list[str] = []
    for line in exported.splitlines():
        if not line.strip():
            continue
        if line[0].isspace() and entries:
            entries[-1] += "\n" + line
        else:
            entries.append(line)

    kept: list[str] = []
    torch_version = ""
    for entry in entries:
        match = REQUIREMENT.match(entry)
        if match is None:
            raise SystemExit(f"unexpected line in poetry export: {entry.splitlines()[0]}")
        name, version = match.group(1).lower().replace("_", "-"), match.group(2)
        if name == "torch":
            torch_version = version
        if CUDA_STACK.match(name) is None:
            kept.append(entry)
    if not torch_version:
        raise SystemExit("torch is missing from the poetry export")
    return kept, torch_version


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Install exactly the poetry.lock set.")
    parser.add_argument(
        "--main-only",
        action="store_true",
        help="main group only; do not install the project (container image)",
    )
    args = parser.parse_args(argv)
    groups = ("--only", "main") if args.main_only else ("--with", "dev")
    root = Path(__file__).resolve().parent.parent
    pip = [sys.executable, "-m", "pip", "install", "--no-deps"]
    with tempfile.TemporaryDirectory() as tmp:
        exported = Path(tmp, "locked.txt")
        poetry = ("poetry", "-C", str(root))
        run(*poetry, "check", "--lock")
        run(*poetry, "export", *groups, "-f", "requirements.txt", "-o", str(exported))
        kept, torch_version = split_requirements(exported.read_text())
        hashed = Path(tmp, "hashed.txt")
        hashed.write_text("\n".join(kept) + "\n")

        run(*pip, "--require-hashes", "-r", str(hashed))
        run(*pip, "--index-url", TORCH_CPU_INDEX, f"torch=={torch_version}")
        if not args.main_only:
            run(*pip, "-e", str(root))
    run(sys.executable, "-m", "pip", "check")


if __name__ == "__main__":
    main()
