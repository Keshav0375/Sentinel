#!/usr/bin/env python3
"""Checks a built dashboard site before it is published (backend task 10.5; ops-dashboard §5/§7).

Bundle: base path and no fixtures. Snapshot: schema-valid, complete, secret-free.

The bundle check runs in web CI and before every Pages publish: index.html exists, every
root-absolute `src`/`href` sits under the base path and exists in the build, at least one
`<base>assets/` file is referenced, and nothing outside `snapshot/` is a source or fixture path or
contains a fixture chain id. The snapshot check (Pages only) validates `snapshot/` against
snapshot.v1 with ledger.py and refuses an empty snapshot when the ops-ledger branch exists.

CLI:
  check_site.py DIST --fixtures DIR [--base /Sentinel/] [--snapshot [--require-events]]
Exit codes: 0 publishable, 1 a problem was found, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import functools
import importlib.util
import json
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

DEFAULT_BASE = "/Sentinel/"
SNAPSHOT_DIR = "snapshot"
SNAPSHOT_FILES = ("meta", "chains", "metrics")
ATTR_RE = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']+)["']""")
EXTERNAL_PREFIXES = ("http://", "https://", "data:", "#", "mailto:")
TEXT_SUFFIXES = frozenset(
    {".html", ".js", ".mjs", ".css", ".json", ".svg", ".txt", ".map", ".webmanifest"}
)
FORBIDDEN_SEGMENTS = frozenset({"fixtures", "src"})
LEDGER_PATH = Path(__file__).resolve().with_name("ledger.py")


@functools.cache
def _ledger() -> ModuleType:
    if "ops_ledger" in sys.modules:
        return sys.modules["ops_ledger"]
    spec = importlib.util.spec_from_file_location("ops_ledger", LEDGER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: the frozen dataclass resolves its postponed annotations here.
    sys.modules["ops_ledger"] = mod
    spec.loader.exec_module(mod)
    return mod


def fixture_markers(fixtures_dir: Path) -> set[str]:
    """The chain_id of every chain in every fixture set's chains.json."""
    if not fixtures_dir.is_dir():
        raise FileNotFoundError(fixtures_dir)
    markers: set[str] = set()
    for path in fixtures_dir.rglob("chains.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for chain in doc.get("chains", []):
            if isinstance(chain, dict) and isinstance(chain.get("chain_id"), str):
                markers.add(chain["chain_id"])
    return markers


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _outside_snapshot(dist: Path) -> list[Path]:
    return sorted(
        p for p in dist.rglob("*") if p.is_file() and p.relative_to(dist).parts[0] != SNAPSHOT_DIR
    )


def check_bundle(dist: Path, *, base: str, markers: Iterable[str]) -> list[str]:
    """Problems with the built bundle: base path, missing assets, fixtures in the build."""
    index = dist / "index.html"
    if not index.is_file():
        return ["index.html is missing"]
    problems: list[str] = []
    refs = ATTR_RE.findall(index.read_text(encoding="utf-8", errors="replace"))
    for ref in refs:
        if ref.startswith(EXTERNAL_PREFIXES):
            continue
        target = ref.split("?", 1)[0].split("#", 1)[0]
        if target.startswith(base):
            if not (dist / target[len(base) :]).is_file():
                problems.append(f"index.html: {ref} is not in the build")
        elif target.startswith("/"):
            problems.append(f"index.html: {ref} is outside the base path {base}")
        else:
            problems.append(f"index.html: {ref} is relative; expected {base}…")
    if not any(ref.startswith(f"{base}assets/") for ref in refs):
        problems.append(f"index.html references no {base}assets/ file (built without base {base}?)")
    ordered = sorted(set(markers))
    for path in _outside_snapshot(dist):
        rel = _rel(path, dist)
        if FORBIDDEN_SEGMENTS & set(path.relative_to(dist).parts):
            problems.append(f"{rel}: source or fixture path in the build")
        if path.suffix in TEXT_SUFFIXES:
            text = path.read_text("utf-8", errors="replace")
            problems += [
                f"{rel}: contains fixture chain id {marker}" for marker in ordered if marker in text
            ]
    return problems


def check_snapshot(dist: Path, *, require_events: bool) -> list[str]:
    """Problems with dist/snapshot/: schema, completeness, secrets, the empty-site guard."""
    snap = dist / SNAPSHOT_DIR
    if not snap.is_dir():
        return [f"{SNAPSHOT_DIR}/ is missing"]
    ledger = _ledger()
    schema = ledger.load_schema(ledger.SNAPSHOT_SCHEMA_PATH)
    problems: list[str] = []

    def _doc(path: Path, expected_file: str) -> tuple[dict[str, Any] | None, list[str]]:
        rel = _rel(path, dist)
        if not path.is_file():
            return None, [f"{rel}: missing"]
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, [f"{rel}: not valid JSON"]
        if not isinstance(doc, dict):
            return None, [f"{rel}: not an object"]
        found = [f"{rel}: {err}" for err in ledger.validate(doc, schema)]
        if doc.get("file") != expected_file:
            found.append(f"{rel}: file is {doc.get('file')!r}, expected {expected_file!r}")
        found += [f"{rel}: secret-like value at {p}" for p in ledger.find_secrets(doc)]
        return doc, found

    docs: dict[str, dict[str, Any] | None] = {}
    for name in SNAPSHOT_FILES:
        docs[name], found = _doc(snap / f"{name}.json", name)
        problems += found
    for path in sorted((snap / "entities").glob("*.json")):
        _, found = _doc(path, "entity")
        problems += found

    chains = docs["chains"] or {}
    for chain in chains.get("chains", []):
        if not isinstance(chain, dict) or not isinstance(chain.get("chain_id"), str):
            continue
        cid = chain["chain_id"]
        if not (snap / "entities" / f"{cid}.json").is_file():
            problems.append(f"{SNAPSHOT_DIR}/entities/{cid}.json: missing")

    meta = docs["meta"]
    if require_events and meta is not None and meta.get("event_count") == 0:
        problems.append(
            "snapshot has no events but the ops-ledger branch exists; "
            "refusing to publish an empty site"
        )
    return problems


def _base(value: str) -> str:
    if not (value.startswith("/") and value.endswith("/")):
        raise argparse.ArgumentTypeError("base must start and end with '/'")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_site", description="Sentinel ops site check")
    parser.add_argument("dist", type=Path)
    parser.add_argument("--base", type=_base, default=DEFAULT_BASE)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--require-events", action="store_true")
    args = parser.parse_args(argv)
    if args.require_events and not args.snapshot:
        parser.error("--require-events needs --snapshot")

    dist: Path = args.dist
    problems: list[str] = []
    if not dist.is_dir():
        problems.append(f"{dist} is not a directory")
    else:
        markers: set[str] = set()
        try:
            markers = fixture_markers(args.fixtures)
        except FileNotFoundError:
            problems.append(
                f"fixtures dir {args.fixtures} is missing (the no-fixture check would be void)"
            )
        problems += check_bundle(dist, base=args.base, markers=markers)
        if args.snapshot:
            problems += check_snapshot(dist, require_events=args.require_events)

    if problems:
        for problem in problems:
            print(f"check-site: {problem}", file=sys.stderr)
        return 1
    files = sum(1 for p in dist.rglob("*") if p.is_file())
    suffix = ", snapshot" if args.snapshot else ""
    print(f"check-site: ok ({files} files, base {args.base}{suffix})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
