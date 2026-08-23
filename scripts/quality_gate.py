#!/usr/bin/env python3
"""Category-aware quality gate for the Sentinel three-repo system.

One entrypoint that runs the right toolchain per repo type. This is the exact body
CI jobs invoke, so local checks and CI never drift (see the sentinel-brain repo,
implementation/README.md §7).

Usage (SIBLINGS=../Sentinel-development-project):
    python scripts/quality_gate.py --repo backend [--path .] [--fast] [--json]
    python scripts/quality_gate.py --repo infra      --path $SIBLINGS/Sentinel-infra
    python scripts/quality_gate.py --repo deployment --path $SIBLINGS/Sentinel-deployment

Exit code 0 = all required checks passed. Non-zero = at least one required check failed.
Checks whose tool is not installed are reported as SKIPPED (warning), not failures,
so the gate runs anywhere; CI images pin the full toolchain so nothing is skipped there.
Path arguments that do not exist yet are pruned before the tool runs (a repo mid-build
has not created every directory), and a check whose paths have all been pruned is
SKIPPED rather than failed. Some tools take no path argument at all and discover their
own inputs — those declare it in IMPLICIT_PATHS so they get the same treatment.

The pytest globs below must cover every `tests/` directory named by a task file in
the sentinel-brain repo's implementation/tasks/. If a phase adds a test package, add it here —
otherwise the gate reports green without ever running that phase's tests.
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
        # Unit = no external service required.
        (
            "pytest-unit",
            [
                "pytest",
                "tests/test_models/",
                "tests/test_tools/",
                "tests/test_providers/",
                "tests/test_config.py",
                "-x",
                "-q",
            ],
            True,
        ),
        # Integration = needs Postgres/pgvector, a fake-LLM pipeline, or the app.
        (
            "pytest-integration",
            [
                "pytest",
                "tests/test_infra/",
                "tests/test_memory/",
                "tests/test_agents/",
                "tests/test_api/",
                "tests/test_eval/",
                "-x",
                "-q",
            ],
            True,
        ),
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
        # `terraform validate` needs an initialized working dir for provider schemas.
        # `-backend=false` keeps the gate offline — no remote state, no Azure auth.
        ("tf-init", ["terraform", "init", "-backend=false", "-input=false", "-no-color"], True),
        ("tf-validate", ["terraform", "validate", "-no-color"], True),
        ("tflint", ["tflint", "--recursive"], False),
        ("tfsec", ["tfsec", "."], False),
        # The bootstrap scripts create the state account and the CI identity by running
        # against a live subscription — the highest-risk files in sentinel-infra and, until
        # now, the only ones no gate check ever read. Required, because a green gate that
        # never lints them says nothing about them. It caught CRLF line endings on the first
        # run, which would have failed every script on ubuntu-latest with
        # "bad interpreter: /usr/bin/env bash^M". Path pruning skips this cleanly in repos
        # that ship no scripts/ (see _is_path_arg, which now recognises .sh).
        (
            "shellcheck",
            ["shellcheck", "scripts/bootstrap-state.sh", "scripts/bootstrap-oidc.sh"],
            True,
        ),
        # Phase 3 put PYTHON in the infra repo: the Event Grid bridge and the KV
        # rotator run unattended against live services, and until this entry the
        # gate never imported them, let alone ran their tests — PASS said nothing
        # about 149 lines of handler code (review blocker, 2026-08-23; same
        # blindspot class as the shellcheck gap). Path-pruned in repos without
        # the tests directory.
        (
            "py-unittest",
            ["python", "-m", "unittest", "discover", "-s", "modules/functions/tests/"],
            True,
        ),
        ("ruff-infra", ["ruff", "check", "modules/functions/src/"], False),
        # sentinel-infra ships 4 workflows (dry / apply / destroy / runners) — lint them here
        # rather than standalone, so infra task 4.3's gate is the same body CI runs.
        ("actionlint", ["actionlint"], True),
        ("yamllint", ["yamllint", ".github/"], False),
        ("gitleaks", ["gitleaks", "detect", "--no-banner", "--redact"], True),
    ],
}

# Checks skipped when --fast is set (slow / network / heavy).
FAST_SKIP = {"pytest-integration", "pip-audit", "tfsec", "gitleaks"}

# Checks whose input path is implicit: the tool discovers its own files instead of
# taking them as argv, so resolve_argv() has nothing to prune. `actionlint` walks
# .github/workflows/ itself and exits 3 when the directory is absent — which is the
# normal state of a repo mid-build, since sentinel-infra ships workflows in task 4.3
# and sentinel-deployment in task 2.2. Without this, infra phases 1-3 and deployment
# phase 1 could never report green no matter what they contained.
IMPLICIT_PATHS = {"actionlint": ".github/workflows"}


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
    def skipped(self) -> list[Result]:
        return [r for r in self.results if r.status == "skipped"]

    @property
    def ran(self) -> list[Result]:
        return [r for r in self.results if r.status != "skipped"]

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def inconclusive(self) -> bool:
        """Nothing actually executed — 'PASS' here would be meaningless.

        Happens when no tool in the matrix is installed, or every path the checks name
        is still missing. The exit code stays 0 (CI images pin the full toolchain, so
        this only fires locally) but the verdict must never read as a clean pass.
        """
        return not self.ran


def _is_path_arg(arg: str) -> bool:
    """True for args that name a file or directory in the repo (not a flag)."""
    if arg.startswith("-"):
        return False
    return arg.endswith((".py", ".sh", "/")) or arg == "."


def resolve_argv(argv: list[str], cwd: Path) -> list[str] | None:
    """Drop path args that don't exist yet.

    A repo mid-build hasn't created every directory a check names (e.g. `tests/`
    before the first test task lands). Running `pytest tests/test_eval/` there is a
    hard error, not a signal. Returns None when the check named paths and none of
    them survived — the caller reports SKIPPED.
    """
    path_args = [a for a in argv if _is_path_arg(a)]
    if not path_args:
        return argv
    kept = [a for a in path_args if (cwd / a).exists()]
    if not kept:
        return None
    dropped = set(path_args) - set(kept)
    return [a for a in argv if a not in dropped]


def run_check(name: str, argv: list[str], required: bool, cwd: Path) -> Result:
    tool = argv[0]
    if shutil.which(tool) is None:
        return Result(name, "skipped", detail=f"{tool} not on PATH")
    implicit = IMPLICIT_PATHS.get(name)
    if implicit is not None and not (cwd / implicit).exists():
        return Result(name, "skipped", detail=f"no such path yet: {implicit}")
    resolved = resolve_argv(argv, cwd)
    if resolved is None:
        paths = " ".join(a for a in argv if _is_path_arg(a))
        return Result(name, "skipped", detail=f"no such path yet: {paths}")
    argv = resolved
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


def init_stdout() -> bool:
    """Make stdout safe for the report glyphs; True if they will render.

    Windows consoles default to cp1252, which cannot encode the box-drawing rules or the
    status emoji — printing them raises UnicodeEncodeError and the gate dies *before* it
    reports anything, which reads as a broken gate rather than a failing check. Try UTF-8
    first; if the stream still can't take the glyphs, fall back to ASCII markers.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):  # pragma: no cover - stream-dependent
        pass
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "─✅⏭".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def print_human(report: Report, unicode_ok: bool = True) -> None:
    if unicode_ok:
        icons = {"pass": "✅", "fail": "❌", "skipped": "⏭️ "}
        rule, sep, corner_top, corner_bottom = "─", "·", "╭─", "╰─"
    else:
        icons = {"pass": "[PASS]", "fail": "[FAIL]", "skipped": "[SKIP]"}
        rule, sep, corner_top, corner_bottom = "-", "|", "+-", "+-"

    print(f"\nQuality gate {sep} repo={report.repo} {sep} path={report.path}\n" + rule * 56)
    for r in report.results:
        line = f"{icons[r.status]} {r.name:<20} {r.seconds:5.1f}s"
        if r.detail and r.status != "pass":
            line += f"  ({r.detail.splitlines()[0][:60]})"
        print(line)
    print(rule * 56)
    tally = f"{len(report.ran)} ran, {len(report.skipped)} skipped"
    if report.inconclusive:
        print(f"RESULT: INCONCLUSIVE {icons['skipped']} ({tally}) — nothing was verified.")
        print("        Install the missing tools, or create the paths, before trusting this.")
    elif report.ok:
        print(f"RESULT: PASS {icons['pass']}  ({tally})")
        if report.skipped:
            names = ", ".join(r.name for r in report.skipped)
            print(f"        NOT verified (skipped): {names}")
    else:
        print(f"RESULT: FAIL {icons['fail']}  ({len(report.failed)} required check(s) failed)")
        for r in report.failed:
            print(f"\n{corner_top} {r.name} " + rule * 33)
            print(r.detail or "(no output captured)")
            print(corner_bottom + rule * 39)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sentinel category-aware quality gate")
    parser.add_argument("--repo", required=True, choices=sorted(MATRIX))
    parser.add_argument("--path", default=".", help="repo working directory (default: cwd)")
    parser.add_argument("--fast", action="store_true", help="skip slow/network checks")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()

    unicode_ok = init_stdout()
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
            "inconclusive": report.inconclusive,
            "ran": len(report.ran),
            "skipped": [r.name for r in report.skipped],
            "results": [r.__dict__ for r in report.results],
        }, indent=2))
    else:
        print_human(report, unicode_ok)

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
