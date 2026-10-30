"""scripts/eval_report_from_logs.py: the eval reports from the eval Job logs (decision 2026-10-28).

run_eval.py --print-report wraps each report file base64-encoded between BEGIN/END markers; the
Job log also carries structlog JSON and plain prints around them. The extractor writes only what a
complete, valid block decodes to and never echoes log content. Offline.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/eval_report_from_logs.py"
JSON = b'{"generated_at": "2026-10-28T09:30:00Z", "summary": {"items_scored": 4}}\n'
MD = "# Sentinel Eval Report\n\n| check | rate |\n|---|---|\n| fired | 100% ✓ |\n".encode()
NOISE_BEFORE = [
    '{"event": "eval_run_start", "dataset": "sentinel-scenario-runs", "level": "info"}',
    "Sentinel Eval Runner",
    "========================================",
    "  Reports written to:",
]
NOISE_AFTER = ['{"event": "eval_run_complete", "items_scored": 4}', "partial line without"]


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_report_from_logs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_report_from_logs"] = module
    spec.loader.exec_module(module)
    return module


def _block(mod: ModuleType, name: str, data: bytes) -> list[str]:
    body = base64.encodebytes(data).decode("ascii").rstrip("\n").splitlines()
    return [
        mod.REPORT_MARKER_BEGIN.format(name=name),
        *body,
        mod.REPORT_MARKER_END.format(name=name),
    ]


def _log(*blocks: list[str], newline: str = "\n") -> str:
    lines = [*NOISE_BEFORE]
    for block in blocks:
        lines += [*block, "interleaved line"]
    lines += NOISE_AFTER
    return newline.join(lines) + newline


def _run(mod: ModuleType, tmp_path: Path, text: str) -> tuple[int, Path]:
    log = tmp_path / "eval-job.log"
    log.write_text(text, encoding="utf-8", newline="")
    out = tmp_path / "reports"
    return mod.main([str(log), str(out)]), out


def _files(out: Path) -> list[str]:
    return sorted(p.name for p in out.iterdir()) if out.exists() else []


def test_extracts_json_and_md(mod: ModuleType, tmp_path: Path) -> None:
    text = _log(_block(mod, "eval_report.json", JSON), _block(mod, "eval_report.md", MD))
    rc, out = _run(mod, tmp_path, text)
    assert rc == 0
    assert (out / "eval_report.json").read_bytes() == JSON
    assert (out / "eval_report.md").read_bytes() == MD


def test_large_report_spans_many_lines(mod: ModuleType, tmp_path: Path) -> None:
    big = json.dumps({"results": [{"i": i, "reasoning": "x" * 50} for i in range(500)]}).encode()
    text = _log(_block(mod, "eval_report.json", big))
    rc, out = _run(mod, tmp_path, text)
    assert rc == 0
    assert (out / "eval_report.json").read_bytes() == big


def test_last_block_wins(mod: ModuleType, tmp_path: Path) -> None:
    second = b'{"second": true}'
    text = _log(_block(mod, "eval_report.json", JSON), _block(mod, "eval_report.json", second))
    rc, out = _run(mod, tmp_path, text)
    assert rc == 0
    assert (out / "eval_report.json").read_bytes() == second


def test_missing_json_block(
    mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out = _run(mod, tmp_path, _log(_block(mod, "eval_report.md", MD)))
    assert rc == 1
    assert "eval_report.json" in capsys.readouterr().err
    assert _files(out) == []


def test_md_optional(mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _run(mod, tmp_path, _log(_block(mod, "eval_report.json", JSON)))
    assert rc == 0
    assert _files(out) == ["eval_report.json"]
    assert "eval_report.md" in capsys.readouterr().err


def test_unterminated(mod: ModuleType, tmp_path: Path) -> None:
    block = _block(mod, "eval_report.json", JSON)[:-1]
    rc, out = _run(mod, tmp_path, _log(block))
    assert rc == 1
    assert _files(out) == []


def test_non_base64_line(
    mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    block = _block(mod, "eval_report.json", JSON)
    block.insert(2, '{"event": "secret-ish interleaved log"}')
    rc, out = _run(mod, tmp_path, _log(block))
    assert rc == 1
    assert _files(out) == []
    err = capsys.readouterr().err
    assert "non-base64" in err
    assert "secret-ish" not in err


def test_invalid_base64(mod: ModuleType, tmp_path: Path) -> None:
    block = _block(mod, "eval_report.json", JSON)
    block[1] = block[1][:-1]  # breaks the padding
    rc, out = _run(mod, tmp_path, _log(block))
    assert rc == 1
    assert _files(out) == []


def test_json_must_be_object(mod: ModuleType, tmp_path: Path) -> None:
    for data in (b"[]", b"not json"):
        rc, out = _run(mod, tmp_path, _log(_block(mod, "eval_report.json", data)))
        assert rc == 1
        assert _files(out) == []


def test_size_cap(mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "MAX_BYTES", 10)
    rc, out = _run(mod, tmp_path, _log(_block(mod, "eval_report.json", JSON)))
    assert rc == 1
    assert _files(out) == []


def test_crlf_lines(mod: ModuleType, tmp_path: Path) -> None:
    text = _log(
        _block(mod, "eval_report.json", JSON), _block(mod, "eval_report.md", MD), newline="\r\n"
    )
    rc, out = _run(mod, tmp_path, text)
    assert rc == 0
    assert (out / "eval_report.json").read_bytes() == JSON
    assert (out / "eval_report.md").read_bytes() == MD


def test_extract_none_without_block(mod: ModuleType) -> None:
    assert mod.extract("\n".join(NOISE_BEFORE), "eval_report.json") is None
    with pytest.raises(mod.ExtractError):
        mod.extract(mod.REPORT_MARKER_BEGIN.format(name="eval_report.json"), "eval_report.json")
