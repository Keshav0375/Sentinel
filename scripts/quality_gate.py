#!/usr/bin/env python3
"""Category-aware quality gate for the Sentinel three-repo system.

One entrypoint that runs the right toolchain per repo type. This is the exact body
CI jobs invoke, so local checks and CI never drift (see
Planning/Phase-2-Implementation/README.md §7).

Usage:
    python scripts/quality_gate.py --repo backend [--path .] [--fast] [--json]
    python scripts/quality_gate.py --repo infra   --path ../Sentinel-development-project/Sentinel-infra
    python scripts/quality_gate.py --repo deployment --path ../Sentinel-development-project/Sentinel-deployment

Exit code 0 = all required checks passed. Non-zero = at least one required check failed.
Checks whose tool is not installed are reported as SKIPPED (warning), not failures,
so the gate runs anywhere; CI images pin the full toolchain so nothing is skipped there.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ── Check matrix ───────────────────────────────────────────────────────────────
# Each check: (name, argv, required). `argv[0]` missing on PATH → SKIPPED.
Check = tuple[str, list[str], bool]

MATRIX: dict[str, list[Check]] = {
    "backend": [
        ("ruff-lint", ["ruff", "check", "src/", "tests/"], True),
        ("ruff-format", ["ruff", "format", "--check", "src/", "tests/"], True),
        ("pyright", ["pyright", "src/"], True),
        ("gitleaks", ["gitleaks", "detect", "--no-banner", "--redact"], True),
        ("pip-audit", ["pip-audit", "--strict"], False),
        ("pytest-unit", ["pytest", "tests/test_tools/", "tests/test_models/", "-x", "-q"], True),
        ("pytest-integration", ["pytest", "tests/test_agents/", "tests/test_api/", "-x", "-q"], True),
    ],
    "deployment": [
        ("ruff-lint", ["ruff", "check", "app/", "tests/"], True),
        ("yamllint", ["yamllint", ".github/"], False),
        ("actionlint", ["actionlint"], True),
        ("gitleaks", ["gitleaks", "detect", "--no-banner", "--redact"], True),
        ("pytest", ["pytest", "tests/", "-x", "-q"], True),
    ],
    "infra": [
        ("tf-fmt", ["terraform", "fmt", "-check", "-recursive"], True),
        ("tf-validate", ["terraform", "validate"], True),
        ("tflint", ["tflint", "--recursive"], False),
        ("tfsec", ["tfsec", "."], False),
        ("gitleaks", ["gitleaks", "detect", "--no-banner", "--redact"], True),
    ],
}

# Checks skipped when --fast is set (slow / network / heavy).
FAST_SKIP = {"pytest-integration", "pip-audit", "tfsec", "gitleaks"}


@dataclass
class Result:
    name: str
    status: str  # "pass" | "fail" | "skipped"
    seconds: float = 0.0
    detail: str = ""


@dataclass
class Report:
    repo: str
    path: str
    results: list[Result] = field(default_factory=list)

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if r.status == "fail"]

    @property
    def ok(self) -> bool:
        return not self.failed


def run_check(name: str, argv: list[str], required: bool, cwd: Path) -> Result:
    tool = argv[0]
    if shutil.which(tool) is None:
        return Result(name, "skipped", detail=f"{tool} not on PATH")
    start = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    except OSError as exc:  # pragma: no cover - defensive
        return Result(name, "fail" if required else "skipped", detail=str(exc))
    elapsed = time.monotonic() - start
    if proc.returncode == 0:
        return Result(name, "pass", elapsed)
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-20:]
    return Result(name, "fail" if required else "skipped", elapsed, "\n".join(tail))


def gate(repo: str, path: Path, fast: bool) -> Report:
    report = Report(repo=repo, path=str(path))
    for name, argv, required in MATRIX[repo]:
        if fast and name in FAST_SKIP:
            report.results.append(Result(name, "skipped", detail="--fast"))
            continue
        report.results.append(run_check(name, argv, required, path))
    return report


def print_human(report: Report) -> None:
    icons = {"pass": "✅", "fail": "❌", "skipped": "⏭️ "}
    print(f"\nQuality gate · repo={report.repo} · path={report.path}\n" + "─" * 56)
    for r in report.results:
        line = f"{icons[r.status]} {r.name:<20} {r.seconds:5.1f}s"
        if r.detail and r.status != "pass":
            line += f"  ({r.detail.splitlines()[0][:60]})"
        print(line)
    print("─" * 56)
    if report.ok:
        print("RESULT: PASS ✅")
    else:
        print(f"RESULT: FAIL ❌  ({len(report.failed)} required check(s) failed)")
        for r in report.failed:
            print(f"\n╭─ {r.name} ─────────────────────────────")
            print(r.detail or "(no output captured)")
            print("╰────────────────────────────────────────")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sentinel category-aware quality gate")
    parser.add_argument("--repo", required=True, choices=sorted(MATRIX))
    parser.add_argument("--path", default=".", help="repo working directory (default: cwd)")
    parser.add_argument("--fast", action="store_true", help="skip slow/network checks")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()

    path = Path(args.path).resolve()
    if not path.exists():
        print(f"error: path does not exist: {path}", file=sys.stderr)
        return 2

    report = gate(args.repo, path, args.fast)

    if args.json:
        print(json.dumps({
            "repo": report.repo,
            "path": report.path,
            "ok": report.ok,
            "results": [r.__dict__ for r in report.results],
        }, indent=2))
    else:
        print_human(report)

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
