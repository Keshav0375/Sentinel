"""scripts/run_eval.py --print-report: the report blocks the eval Job prints (decision 2026-10-28).

The blocks round-trip through scripts/eval_report_from_logs.py, which carries the same markers.
Offline: nothing here runs the eval.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cli() -> ModuleType:
    return _load("run_eval_cli", SCRIPTS / "run_eval.py")


@pytest.fixture(scope="module")
def extractor() -> ModuleType:
    return _load("run_eval_cli_extractor", SCRIPTS / "eval_report_from_logs.py")


def test_markers_match_the_extractor(cli: ModuleType, extractor: ModuleType) -> None:
    assert cli.REPORT_MARKER_BEGIN == extractor.REPORT_MARKER_BEGIN
    assert cli.REPORT_MARKER_END == extractor.REPORT_MARKER_END
    assert "{name}" in cli.REPORT_MARKER_BEGIN
    assert "{name}" in cli.REPORT_MARKER_END


def test_print_report_blocks_roundtrip(
    cli: ModuleType,
    extractor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    json_path = tmp_path / "eval_report.json"
    md_path = tmp_path / "eval_report.md"
    json_bytes = ('{"summary": {"items_scored": 4}, "pad": "' + "x" * 400 + '"}\n').encode()
    md_bytes = "# Sentinel Eval Report\n\n✓ fired 100%\n".encode()
    json_path.write_bytes(json_bytes)
    md_path.write_bytes(md_bytes)

    cli._print_report_blocks([json_path, md_path])
    out = capsys.readouterr().out

    assert extractor.extract(out, "eval_report.json") == json_bytes
    assert extractor.extract(out, "eval_report.md") == md_bytes
    markers = {
        cli.REPORT_MARKER_BEGIN.format(name=n) for n in ("eval_report.json", "eval_report.md")
    } | {cli.REPORT_MARKER_END.format(name=n) for n in ("eval_report.json", "eval_report.md")}
    body = [line for line in out.splitlines() if line not in markers]
    assert len(body) > 2
    assert all(len(line) <= 76 for line in body)


def test_flag_parsed(cli: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_eval.py", "--branches", "x", "--print-report"])
    assert cli._parse_args().print_report is True
    monkeypatch.setattr(sys, "argv", ["run_eval.py", "--branches", "x"])
    assert cli._parse_args().print_report is False
