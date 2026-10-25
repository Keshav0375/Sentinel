"""Tests for scripts/install_locked.py — the lock-based installer (CI and the container image)."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

# scripts/ is not on the test Python path — load via importlib.
_SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "install_locked.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("install_locked_script", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_script: Any = _load_script()

_EXPORT = """\
anyio==4.9.0 ; python_version >= "3.12" \\
    --hash=sha256:aaaa \\
    --hash=sha256:bbbb
torch==2.14.1 ; python_version >= "3.12" \\
    --hash=sha256:cccc
nvidia-cublas-cu12==12.8.4.1 ; platform_system == "Linux" \\
    --hash=sha256:dddd
triton==3.5.0 ; platform_system == "Linux" \\
    --hash=sha256:eeee
"""

# ── Helpers ───────────────────────────────────────────────────────────────────


def _fake_run(calls: list[tuple[str, ...]], hashed: list[str]) -> Callable[..., None]:
    def run(*argv: str) -> None:
        calls.append(argv)
        if "export" in argv:
            Path(argv[argv.index("-o") + 1]).write_text(_EXPORT)
        if "--require-hashes" in argv:
            hashed.append(Path(argv[argv.index("-r") + 1]).read_text())

    return run


def _pair(argv: tuple[str, ...], first: str, second: str) -> bool:
    return any(a == first and b == second for a, b in zip(argv, argv[1:], strict=False))


# ── main ──────────────────────────────────────────────────────────────────────


def test_main_only_exports_main_group_and_skips_project(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    hashed: list[str] = []
    monkeypatch.setattr(_script, "run", _fake_run(calls, hashed))
    _script.main(["--main-only"])
    export = next(c for c in calls if "export" in c)
    assert _pair(export, "--only", "main")
    assert "--with" not in export
    assert all("-e" not in c for c in calls)
    assert (
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--index-url",
        _script.TORCH_CPU_INDEX,
        "torch==2.14.1",
    ) in calls
    assert "anyio==4.9.0" in hashed[0]
    for dropped in ("torch==", "nvidia-", "triton=="):
        assert dropped not in hashed[0]
    assert calls[-1] == (sys.executable, "-m", "pip", "check")


def test_default_exports_dev_and_installs_project_editable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    hashed: list[str] = []
    monkeypatch.setattr(_script, "run", _fake_run(calls, hashed))
    _script.main([])
    export = next(c for c in calls if "export" in c)
    assert _pair(export, "--with", "dev")
    editable = [c for c in calls if c[-2:] == ("-e", str(_SCRIPT_PATH.resolve().parent.parent))]
    assert len(editable) == 1
    assert calls[-1] == (sys.executable, "-m", "pip", "check")


# ── split_requirements ────────────────────────────────────────────────────────


def test_split_requirements_drops_cuda_stack() -> None:
    kept, version = _script.split_requirements(_EXPORT)
    assert version == "2.14.1"
    assert len(kept) == 1
    assert kept[0].startswith("anyio==4.9.0")
    assert "--hash=sha256:" in kept[0]


def test_split_requirements_without_torch_exits() -> None:
    with pytest.raises(SystemExit, match="torch is missing"):
        _script.split_requirements(
            'anyio==4.9.0 ; python_version >= "3.12" \\\n    --hash=sha256:aaaa\n'
        )
