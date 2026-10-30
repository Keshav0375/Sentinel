#!/usr/bin/env python3
"""Recover the eval reports from the in-cluster eval Job's logs (decision 2026-10-28).

ci_eval.yml runs the eval as a Kubernetes Job; ``scripts/run_eval.py --print-report`` prints each
report file base64-encoded between fixed BEGIN/END marker lines. This script reads the saved Job
log on the runner and writes ``eval_report.json`` (required) and ``eval_report.md`` (optional) into
the output directory. The last block of a name wins. Stdlib only.

Usage::

    python3 scripts/eval_report_from_logs.py <log_file> <out_dir>

Exit status: 0 when the JSON report was written, 1 otherwise. Messages never echo log content.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
from pathlib import Path

# Verbatim copies of scripts/run_eval.py's markers (tests/test_scripts/test_run_eval_cli.py keeps
# them equal).
REPORT_MARKER_BEGIN = "----- BEGIN SENTINEL EVAL REPORT {name} -----"
REPORT_MARKER_END = "----- END SENTINEL EVAL REPORT {name} -----"
NAMES = ("eval_report.json", "eval_report.md")
MAX_BYTES = 10_000_000
B64_LINE = re.compile(r"^[A-Za-z0-9+/=]*$")


class ExtractError(Exception):
    """A report block is present but cannot be decoded (message names no content)."""


def extract(log_text: str, name: str) -> bytes | None:
    """The decoded bytes of the last *name* block in *log_text*; None when there is none."""
    begin = REPORT_MARKER_BEGIN.format(name=name)
    end = REPORT_MARKER_END.format(name=name)
    lines = [line.rstrip("\r\n ") for line in log_text.splitlines()]
    starts = [i for i, line in enumerate(lines) if line == begin]
    if not starts:
        return None
    start = starts[-1]
    stop = next((i for i in range(start + 1, len(lines)) if lines[i] == end), None)
    if stop is None:
        raise ExtractError("unterminated block")
    body = lines[start + 1 : stop]
    if any(not B64_LINE.match(line) for line in body):
        raise ExtractError("non-base64 line in block")
    try:
        data = base64.b64decode("".join(body), validate=True)
    except binascii.Error:
        raise ExtractError("invalid base64 in block") from None
    if len(data) > MAX_BYTES:
        raise ExtractError("block exceeds the size cap")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval_report_from_logs.py", description="Recover the eval reports from Job logs"
    )
    parser.add_argument("log_file", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args(argv)
    json_name, md_name = NAMES
    try:
        log_text = args.log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print("error: the Job log is unreadable", file=sys.stderr)
        return 1
    try:
        report = extract(log_text, json_name)
    except ExtractError as exc:
        print(f"error: {json_name}: {exc}", file=sys.stderr)
        return 1
    if report is None:
        print(f"error: {json_name} block not found", file=sys.stderr)
        return 1
    try:
        parsed = json.loads(report)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        print(f"error: {json_name} is not a JSON object", file=sys.stderr)
        return 1
    try:
        markdown = extract(log_text, md_name)
    except ExtractError as exc:
        print(f"warning: {md_name}: {exc}; not written", file=sys.stderr)
        markdown = None
    else:
        if markdown is None:
            print(f"warning: {md_name} block not found", file=sys.stderr)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / json_name).write_bytes(report)
    if markdown is not None:
        (args.out_dir / md_name).write_bytes(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
