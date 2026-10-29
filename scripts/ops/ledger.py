#!/usr/bin/env python3
"""Ops ledger: build, validate and append ops.v1 events (backend task 10.1).

The ops dashboard (architecture/ops-dashboard.md) reads an append-only ledger: one JSON file per
event on the orphan `ops-ledger` branch of Keshav0375/Sentinel (§2), every file valid against the
frozen envelope in `schema/event.v1.json` (§3), written by the `ops-event` composite action and
the reconciler. Ledger data is public, so writes fail closed (§5): fields outside the per-kind
allow-list are dropped, a secret-like value rejects the event, free text is capped at 500
characters, and the result must validate before anything is pushed.

Transport is git plumbing against a fresh temp repo: no checkout, the workspace is never read.
Each attempt shallow-fetches the branch tip, rebuilds the one-file commit on it and pushes without
force. Rebuilding the single-file commit on the new tip is exactly `git pull --rebase`, so a push
that races is retried on the winner's tip (bounded, `MAX_PUSH_ATTEMPTS`). Idempotency is by
event_id anywhere under `events/`: a fact already recorded is reported as `exists`. When the
branch is absent it is created as a parentless root holding README.md and the schemas.

The validator is hand-rolled (stdlib only; the action runs on the runner's bare python3) and
supports exactly the JSON Schema keywords the schemas use; any other keyword is a SchemaError.

CLI:
  ledger.py append              reads OPS_EVENT_KIND, OPS_EVENT_SOURCE, OPS_EVENT_SOURCE_ID,
                                OPS_EVENT_STATE, OPS_EVENT_TS, OPS_EVENT_CHAIN, OPS_EVENT_DATA,
                                OPS_LEDGER_TOKEN, OPS_LEDGER_REMOTE; prints event-id= and result=
  ledger.py validate FILE...    validate event files against the event schema
Exit codes: 0 ok, 2 invalid input or event, 3 push failed after every attempt.
"""

from __future__ import annotations

import argparse
import base64
import functools
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "ops.v1"
LEDGER_BRANCH = "ops-ledger"
LEDGER_REMOTE = "https://github.com/Keshav0375/Sentinel.git"
SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
EVENT_SCHEMA_PATH = SCHEMA_DIR / "event.v1.json"
SNAPSHOT_SCHEMA_PATH = SCHEMA_DIR / "snapshot.v1.json"

# R14: only these free-text fields are truncated; every other string is bounded by the schema.
TEXT_CAP = 500
CAPPED_TEXT: dict[str, tuple[str, ...]] = {
    "agent.decided": ("root_cause_summary",),
    "escalation.raised": ("summary",),
}

MAX_PUSH_ATTEMPTS = 5
# One append is bounded in total (attempts + backoff sleeps share the budget) and per git call;
# every per-call timeout is capped at what is left of the budget.
APPEND_DEADLINE_S = 120.0
GIT_NETWORK_TIMEOUT_S = 30.0
GIT_LOCAL_TIMEOUT_S = 10.0
NETWORK_OPS = frozenset({"ls-remote", "fetch", "push"})
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"

LEDGER_README = """\
# ops-ledger

Append-only event ledger for the Sentinel ops dashboard (backend phase 10).

- One JSON file per event: `events/YYYY/MM/DD/<YYYYMMDDTHHMMSSZ>-<kind>-<event_id>.json`.
- Every event is valid against `schema/event.v1.json` (`ops.v1`).
- Files are never edited or deleted; git history is the audit log.
- Written only by GitHub Actions (`.github/actions/ops-event`) and the reconciler.
- Public data: no logs, tool output, secrets, tokens, keys or IP addresses.
"""

SUPPORTED_KEYWORDS = frozenset(
    {
        "$ref",
        "type",
        "enum",
        "const",
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "allOf",
        "anyOf",
        "if",
        "then",
    }
)
ANNOTATION_KEYWORDS = frozenset({"$schema", "$id", "$comment", "$defs", "title", "description"})

_SECRET_SOURCES: tuple[tuple[str, str], ...] = (
    ("github_token", r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36,}"),
    ("github_pat", r"github_pat_[A-Za-z0-9_]{22,}"),
    ("llm_api_key", r"(?<![A-Za-z0-9])sk-(?:ant-)?[A-Za-z0-9_-]{20,}"),
    ("langfuse_key", r"(?<![A-Za-z0-9])[ps]k-lf-[A-Za-z0-9-]{8,}"),
    ("aws_key", r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}"),
    ("jwt", r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("private_key", r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY"),
    ("slack_token", r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    ("url_credentials", r"[A-Za-z][A-Za-z0-9+.-]{0,20}://[^\s/:@]{1,200}:[^\s/@]{1,200}@"),
    ("azure_key", r"(?i)(?:AccountKey=|SharedAccessKey=|[?&]sig=[A-Za-z0-9%+/=]{16,})"),
    ("azure_client_secret", r"[A-Za-z0-9_~.-]{3}8Q~[A-Za-z0-9_~.-]{31,34}"),
    ("bearer", r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    (
        "assignment",
        r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token)\s*[=:]\s*\S{6,}",
    ),
    ("webhook", r"(?i)(?:webhook\.office\.com/|\.logic\.azure\.com[:/])"),
    ("ipv4", r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])"),
    ("ipv6", r"(?i)(?<![0-9a-f:])(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}(?![0-9a-f:])"),
)
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(source)) for name, source in _SECRET_SOURCES
)

_REF = re.compile(r"^#/\$defs/[A-Za-z0-9_]+$")
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_sleep = time.sleep  # looked up at call time so tests can monkeypatch ledger._sleep
_monotonic = time.monotonic  # likewise ledger._monotonic

# A push the remote refused because the tip moved: rebuild on the new tip and retry.
NON_FF_RE = re.compile(
    r"non-fast-forward|fetch first|stale info|cannot lock ref|incorrect old value", re.I
)
# A push refused for auth or branch protection: retrying cannot help.
FATAL_PUSH_RE = re.compile(
    r"\[remote rejected\]|\[rejected\]|permission to \S+ denied|authentication failed"
    r"|could not read username|returned error: 40[134]|protected branch|GH0\d\d"
    r"|refusing to allow|write access to repository not granted|invalid username or password",
    re.I,
)


# ── Errors ────────────────────────────────────────────────────────────────────


class LedgerError(Exception):
    """Base error. Messages name the field or path, never a value."""


class SchemaError(LedgerError):
    """The schema itself uses something the validator does not support."""


class SchemaValidationError(LedgerError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("event failed schema validation: " + "; ".join(errors[:10]))


class SecretDetectedError(LedgerError):
    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__("secret-like value in: " + ", ".join(paths))


class LedgerGitError(LedgerError):
    """One git call failed (retryable inside append)."""


class LedgerPushError(LedgerError):
    """Every push attempt failed."""


class LedgerPushRejectedError(LedgerPushError):
    """push rejected (auth or protection); not retried."""


@dataclass(frozen=True)
class AppendResult:
    event_id: str
    path: str
    status: str  # "written" | "exists"
    attempts: int


# ── Identity and timestamps ───────────────────────────────────────────────────


def compute_event_id(kind: str, source: str, source_id: str, state: str = "") -> str:
    """sha256 of kind, source, source_id and state joined by LF (§3)."""
    parts = {"kind": kind, "source": source, "source_id": source_id, "state": state}
    for name, value in parts.items():
        if not isinstance(value, str):
            raise LedgerError(f"{name} must be a string")
        if "\n" in value:
            raise LedgerError(f"{name} contains a line break")
        if not value and name != "state":
            raise LedgerError(f"{name} is empty")
    return hashlib.sha256("\n".join([kind, source, source_id, state]).encode("utf-8")).hexdigest()


def format_ts(value: datetime | str) -> str:
    """Normalise an aware datetime or an RFC 3339 string to `YYYY-MM-DDTHH:MM:SSZ`."""
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise LedgerError("ts is not an RFC 3339 timestamp") from None
    elif isinstance(value, datetime):
        dt = value
    else:
        raise LedgerError("ts is not an RFC 3339 timestamp")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise LedgerError("ts has no UTC offset")
    return dt.astimezone(UTC).strftime(_TS_FORMAT)


# ── Schema loading and validation ─────────────────────────────────────────────


def _check_node(node: Any, root: Mapping[str, Any], where: str) -> None:
    if not isinstance(node, Mapping):
        raise SchemaError(f"schema node at {where} is not an object")
    for key, value in node.items():
        known = key in SUPPORTED_KEYWORDS or key in ANNOTATION_KEYWORDS or key.startswith("x-")
        if not known:
            raise SchemaError(f"unsupported keyword {key}")
        if key == "$ref":
            if not isinstance(value, str) or not _REF.match(value):
                raise SchemaError(f"unsupported $ref at {where}")
            if value[len("#/$defs/") :] not in (root.get("$defs") or {}):
                raise SchemaError(f"unresolved $ref at {where}")
        elif key == "additionalProperties":
            if not isinstance(value, bool):
                raise SchemaError(f"additionalProperties at {where} must be a boolean")
        elif key in ("properties", "$defs"):
            if not isinstance(value, Mapping):
                raise SchemaError(f"{key} at {where} is not an object")
            for name, child in value.items():
                _check_node(child, root, f"{where}/{key}/{name}")
        elif key in ("items", "if", "then"):
            _check_node(value, root, f"{where}/{key}")
        elif key in ("allOf", "anyOf"):
            if not isinstance(value, list):
                raise SchemaError(f"{key} at {where} is not an array")
            for i, child in enumerate(value):
                _check_node(child, root, f"{where}/{key}/{i}")


def check_schema(schema: Mapping[str, Any]) -> None:
    """Raise SchemaError unless every node uses only the supported keywords."""
    _check_node(schema, schema, "#")


@functools.lru_cache(maxsize=4)
def _load(path_str: str) -> dict[str, Any]:
    with open(path_str, encoding="utf-8") as fh:
        schema = json.load(fh)
    check_schema(schema)
    return schema


def load_schema(path: Path = EVENT_SCHEMA_PATH) -> dict[str, Any]:
    return _load(str(path))


@functools.lru_cache(maxsize=256)
def _regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "integer": lambda v: (
        (isinstance(v, int) and not isinstance(v, bool))
        or (isinstance(v, float) and v.is_integer())
    ),
    "number": _is_number,
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "string": lambda v: isinstance(v, str),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def _json_equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return bool(a == b)


def _resolve(root: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not _REF.match(ref):
        raise SchemaError("unsupported $ref")
    target = (root.get("$defs") or {}).get(ref[len("#/$defs/") :])
    if not isinstance(target, Mapping):
        raise SchemaError("unresolved $ref")
    return target


def validate(
    instance: Any,
    schema: Mapping[str, Any],
    *,
    root: Mapping[str, Any] | None = None,
    path: str = "",
) -> list[str]:
    """Validate `instance`; return error strings `<json pointer>: <msg>` (never a value)."""
    root = schema if root is None else root
    where = path or "/"
    errors: list[str] = []

    if "type" in schema:
        declared = schema["type"]
        names = [declared] if isinstance(declared, str) else list(declared)
        for name in names:
            if name not in _TYPE_CHECKS:
                raise SchemaError(f"unsupported type {name}")
        if not any(_TYPE_CHECKS[name](instance) for name in names):
            return [f"{where}: expected {' or '.join(names)}"]

    if "$ref" in schema:
        errors += validate(instance, _resolve(root, schema["$ref"]), root=root, path=path)

    if "enum" in schema and not any(_json_equal(instance, v) for v in schema["enum"]):
        errors.append(f"{where}: not an allowed value")
    if "const" in schema and not _json_equal(instance, schema["const"]):
        errors.append(f"{where}: not an allowed value")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{where}: too short")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{where}: too long")
        if "pattern" in schema and not _regex(schema["pattern"]).search(instance):
            errors.append(f"{where}: does not match the required pattern")

    if _is_number(instance):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{where}: below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{where}: above maximum")

    if isinstance(instance, dict):
        for name in schema.get("required", ()):
            if name not in instance:
                errors.append(f"{where}: missing field {name}")
        properties: Mapping[str, Any] = schema.get("properties") or {}
        for name, child in properties.items():
            if name in instance:
                errors += validate(instance[name], child, root=root, path=f"{path}/{name}")
        if schema.get("additionalProperties") is False:
            for name in instance:
                if name not in properties:
                    errors.append(f"{where}: unexpected field {name}")

    if isinstance(instance, list):
        if "items" in schema:
            for i, item in enumerate(instance):
                errors += validate(item, schema["items"], root=root, path=f"{path}/{i}")
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{where}: too few items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{where}: too many items")

    for sub in schema.get("allOf", ()):
        errors += validate(instance, sub, root=root, path=path)
    if "anyOf" in schema and not any(
        not validate(instance, sub, root=root, path=path) for sub in schema["anyOf"]
    ):
        errors.append(f"{where}: matches none of the allowed shapes")
    if "if" in schema and not validate(instance, schema["if"], root=root, path=path):
        if "then" in schema:
            errors += validate(instance, schema["then"], root=root, path=path)

    return errors


def allowed_data_fields(kind: str, schema: Mapping[str, Any] | None = None) -> frozenset[str]:
    """Property names of `kind`'s data def (the allow-list)."""
    schema = load_schema() if schema is None else schema
    for branch in schema.get("allOf", ()):
        kind_rule = branch.get("if", {}).get("properties", {}).get("kind", {})
        if "const" in kind_rule and _json_equal(kind_rule["const"], kind):
            data_def = _resolve(schema, branch["then"]["properties"]["data"]["$ref"])
            return frozenset(data_def.get("properties") or {})
    raise SchemaValidationError(["/kind: not an allowed value"])


def allowed_chain_fields(schema: Mapping[str, Any] | None = None) -> frozenset[str]:
    schema = load_schema() if schema is None else schema
    return frozenset(schema["$defs"]["chain"]["properties"])


# ── Secrets and the text cap ──────────────────────────────────────────────────


def find_secrets(value: Any, path: str = "") -> list[str]:
    """Paths (`data.spans[0].name`) of every string that looks like a secret."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            found += find_secrets(child, f"{path}.{key}".lstrip("."))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            found += find_secrets(child, f"{path}[{i}]")
    elif isinstance(value, str):
        if any(pattern.search(value) for _, pattern in SECRET_PATTERNS):
            found.append(path)
    return found


def cap_text(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    capped = dict(data)
    for name in CAPPED_TEXT.get(kind, ()):
        text = capped.get(name)
        if isinstance(text, str) and len(text) > TEXT_CAP:
            capped[name] = text[: TEXT_CAP - 1] + "…"
    return capped


# ── Events ────────────────────────────────────────────────────────────────────


def build_event(
    *,
    kind: str,
    source: str,
    source_id: str,
    state: str = "",
    ts: datetime | str | None = None,
    chain: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    recorded_at: datetime | None = None,
    schema: Mapping[str, Any] | None = None,
    warn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Build one ops.v1 event: strip, scan, cap, stamp and validate (fails closed)."""
    schema = load_schema() if schema is None else schema
    if kind not in schema["properties"]["kind"]["enum"]:
        raise SchemaValidationError(["/kind: not an allowed value"])
    chain = {} if chain is None else chain
    data = {} if data is None else data
    if not isinstance(chain, Mapping):
        raise SchemaValidationError(["/chain: expected object"])
    if not isinstance(data, Mapping):
        raise SchemaValidationError(["/data: expected object"])

    allowed_data = allowed_data_fields(kind, schema)
    allowed_chain = allowed_chain_fields(schema)
    dropped: list[str] = []
    clean_data: dict[str, Any] = {}
    for key, value in data.items():
        if value is None:
            continue
        if key in allowed_data:
            clean_data[key] = value
        else:
            dropped.append(f"data.{key}")
    clean_chain: dict[str, Any] = {}
    for key, value in chain.items():
        if value is None:
            continue
        if key in allowed_chain:
            clean_chain[key] = value
        else:
            dropped.append(f"chain.{key}")
    if dropped and warn is not None:
        warn("dropped unknown fields: " + ", ".join(sorted(dropped)))

    secrets = find_secrets({"chain": clean_chain, "data": clean_data})
    if secrets:
        raise SecretDetectedError(secrets)
    clean_data = cap_text(kind, clean_data)

    recorded = recorded_at or datetime.now(UTC)
    event: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "event_id": compute_event_id(kind, source, source_id, state),
        "kind": kind,
        "ts": format_ts(ts if ts is not None else recorded),
        "recorded_at": format_ts(recorded),
        "source": source,
        "chain": clean_chain,
        "data": clean_data,
    }
    errors = validate(event, schema)
    if errors:
        raise SchemaValidationError(errors)
    return event


def event_path(event: Mapping[str, Any]) -> str:
    """`events/YYYY/MM/DD/<YYYYMMDDTHHMMSSZ>-<kind>-<event_id>.json`, dated by `ts`."""
    try:
        ts = datetime.strptime(str(event["ts"]), _TS_FORMAT)
    except (KeyError, ValueError):
        raise LedgerError("ts is not an RFC 3339 timestamp") from None
    stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    return f"events/{ts:%Y/%m/%d}/{stamp}-{event['kind']}-{event['event_id']}.json"


def serialize(event: Mapping[str, Any]) -> bytes:
    return (json.dumps(event, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def bootstrap_files() -> dict[str, bytes]:
    return {
        "README.md": LEDGER_README.encode(),
        "schema/event.v1.json": EVENT_SCHEMA_PATH.read_bytes(),
        "schema/snapshot.v1.json": SNAPSHOT_SCHEMA_PATH.read_bytes(),
    }


# ── Git transport ─────────────────────────────────────────────────────────────


def _git_env(token: str | None) -> dict[str, str]:
    """Hermetic git env; the token travels only as an http header scoped to github.com."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": BOT_NAME,
            "GIT_AUTHOR_EMAIL": BOT_EMAIL,
            "GIT_COMMITTER_NAME": BOT_NAME,
            "GIT_COMMITTER_EMAIL": BOT_EMAIL,
        }
    )
    if token:
        b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {b64}",
            }
        )
    return env


def op_timeout(op: str, deadline: float | None, now: float) -> float:
    """The timeout for one git call: per-op, capped at what is left of the append deadline."""
    timeout = GIT_NETWORK_TIMEOUT_S if op in NETWORK_OPS else GIT_LOCAL_TIMEOUT_S
    if deadline is None:
        return timeout
    left = deadline - now
    if left <= 0:
        raise LedgerGitError(f"git {op} failed: append deadline reached")
    return min(timeout, left)


class _Git:
    def __init__(self, cwd: Path, token: str | None, deadline: float | None = None) -> None:
        self.cwd = cwd
        self.env = _git_env(token)
        self.deadline = deadline
        self._redact: list[str] = []
        if token:
            self._redact = [token, base64.b64encode(f"x-access-token:{token}".encode()).decode()]

    def _call(
        self, args: tuple[str, ...], stdin: bytes | None, env_extra: Mapping[str, str] | None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args],
            cwd=self.cwd,
            env={**self.env, **(env_extra or {})},
            input=stdin,
            capture_output=True,
            timeout=op_timeout(args[0], self.deadline, _monotonic()),
            check=False,
        )

    def redact(self, text: str) -> str:
        for secret in self._redact:
            text = text.replace(secret, "***")
        return text

    def run(
        self, *args: str, stdin: bytes | None = None, env_extra: Mapping[str, str] | None = None
    ) -> str:
        try:
            proc = self._call(args, stdin, env_extra)
        except subprocess.TimeoutExpired:
            raise LedgerGitError(f"git {args[0]} failed: timed out") from None
        if proc.returncode != 0:
            lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            detail = self.redact(lines[-1] if lines else f"exit {proc.returncode}")
            raise LedgerGitError(f"git {args[0]} failed: {detail}")
        return proc.stdout.decode("utf-8", "replace").strip()

    def attempt(self, *args: str) -> tuple[int, str]:
        """(returncode, stdout + stderr, redacted); a timeout is (124, "timed out")."""
        try:
            proc = self._call(args, None, None)
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        return proc.returncode, self.redact(output)

    def ok(self, *args: str) -> bool:
        try:
            return self._call(args, None, None).returncode == 0
        except subprocess.TimeoutExpired:
            return False


def _fetch_tip(git: _Git, remote: str, branch: str) -> str | None:
    if not git.run("ls-remote", "--heads", remote, f"refs/heads/{branch}"):
        return None
    git.run(
        "fetch", "-q", "--depth=1", "--no-tags", remote, f"+refs/heads/{branch}:refs/ledger/tip"
    )
    return git.run("rev-parse", "refs/ledger/tip")


def _event_exists(git: _Git, tip: str, event_id: str) -> bool:
    suffix = f"-{event_id}.json"
    listing = git.run("ls-tree", "-r", "--name-only", tip, "--", "events/")
    return any(line.endswith(suffix) for line in listing.splitlines())


def _add_blob(git: _Git, path: str, content: bytes) -> None:
    blob = git.run("hash-object", "-w", "--stdin", stdin=content)
    git.run("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")


def _commit(git: _Git, parent: str | None, message: str) -> str:
    tree = git.run("write-tree")
    parents = ["-p", parent] if parent else []
    return git.run("commit-tree", tree, *parents, "--no-gpg-sign", "-m", message)


def _bootstrap_commit(git: _Git) -> str:
    git.run("read-tree", "--empty")
    for path, content in bootstrap_files().items():
        _add_blob(git, path, content)
    return _commit(git, None, "ops-ledger: initialise")


def _push(git: _Git, remote: str, commit: str, branch: str) -> bool:
    """True pushed; False the tip moved (retry). A refusal for auth or protection raises
    LedgerPushRejectedError (not retried); anything else is a retryable LedgerGitError."""
    code, output = git.attempt("push", "-q", remote, f"{commit}:refs/heads/{branch}")
    if code == 0:
        return True
    if NON_FF_RE.search(output):
        return False
    lines = output.strip().splitlines()
    fatal = next((line for line in lines if FATAL_PUSH_RE.search(line)), None)
    if fatal is not None:
        raise LedgerPushRejectedError(f"push to {branch} rejected: {fatal.strip()[:200]}")
    detail = lines[-1].strip() if lines else f"exit {code}"
    raise LedgerGitError(f"git push failed: {detail}")


def append(
    event: Mapping[str, Any],
    *,
    remote: str = LEDGER_REMOTE,
    token: str | None = None,
    branch: str = LEDGER_BRANCH,
    max_attempts: int = MAX_PUSH_ATTEMPTS,
    sleep: Callable[[float], None] | None = None,
    schema: Mapping[str, Any] | None = None,
    deadline_s: float = APPEND_DEADLINE_S,
) -> AppendResult:
    """Append `event` to the ledger branch; idempotent by event_id, bounded retry on races.

    Attempts and backoff share one `deadline_s` budget; a push refused for auth or protection
    raises LedgerPushRejectedError at once."""
    schema = load_schema() if schema is None else schema
    errors = validate(event, schema)
    if errors:
        raise SchemaValidationError(errors)
    secrets = find_secrets({"chain": event.get("chain"), "data": event.get("data")})
    if secrets:
        raise SecretDetectedError(secrets)

    event_id = str(event["event_id"])
    kind = str(event["kind"])
    payload = serialize(event)
    path = event_path(event)
    last = ""
    attempts = 0
    expired = False
    deadline = _monotonic() + deadline_s
    with tempfile.TemporaryDirectory(prefix="ops-ledger-") as tmp:
        git = _Git(Path(tmp), token, deadline)
        git.run("init", "-q")
        for attempt in range(1, max_attempts + 1):
            if _monotonic() >= deadline:
                expired = True
                break
            attempts = attempt
            try:
                tip = _fetch_tip(git, remote, branch)
                if tip and _event_exists(git, tip, event_id):
                    return AppendResult(event_id, path, "exists", attempt)
                parent = tip or _bootstrap_commit(git)
                git.run("read-tree", parent)
                _add_blob(git, path, payload)
                commit = _commit(git, parent, f"ops: {kind} {event_id[:12]}")
                if _push(git, remote, commit, branch):
                    return AppendResult(event_id, path, "written", attempt)
            except LedgerGitError as exc:
                last = str(exc)
            if attempt < max_attempts:
                backoff = min(2 ** (attempt - 1), 8) + random.uniform(0, 0.5)
                (sleep or _sleep)(min(backoff, max(0.0, deadline - _monotonic())))
    budget = f" (deadline {deadline_s:g} s reached)" if expired else ""
    raise LedgerPushError(
        f"append to {branch} failed after {attempts} attempts{budget}"
        + (f": {last}" if last else "")
    )


# ── CLI ───────────────────────────────────────────────────────────────────────


def _err(message: str) -> None:
    print(f"ops-ledger: {message}", file=sys.stderr)


def _env_object(name: str) -> dict[str, Any] | None:
    try:
        value = json.loads(os.environ.get(name) or "{}")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _cmd_append() -> int:
    for name in ("OPS_EVENT_KIND", "OPS_EVENT_SOURCE", "OPS_EVENT_SOURCE_ID"):
        if not os.environ.get(name):
            _err(f"{name} is required")
            return 2
    objects: dict[str, dict[str, Any]] = {}
    for name in ("OPS_EVENT_CHAIN", "OPS_EVENT_DATA"):
        value = _env_object(name)
        if value is None:
            _err(f"{name} is not a JSON object")
            return 2
        objects[name] = value
    try:
        event = build_event(
            kind=os.environ["OPS_EVENT_KIND"],
            source=os.environ["OPS_EVENT_SOURCE"],
            source_id=os.environ["OPS_EVENT_SOURCE_ID"],
            state=os.environ.get("OPS_EVENT_STATE") or "",
            ts=os.environ.get("OPS_EVENT_TS") or None,
            chain=objects["OPS_EVENT_CHAIN"],
            data=objects["OPS_EVENT_DATA"],
            warn=_err,
        )
        result = append(
            event,
            remote=os.environ.get("OPS_LEDGER_REMOTE") or LEDGER_REMOTE,
            token=os.environ.get("OPS_LEDGER_TOKEN") or None,
        )
    except LedgerPushError as exc:
        _err(str(exc))
        return 3
    except LedgerError as exc:
        _err(str(exc))
        return 2
    print(f"event-id={result.event_id}")
    print(f"result={result.status}")
    return 0


def _cmd_validate(files: list[str]) -> int:
    schema = load_schema()
    failed = False
    for name in files:
        try:
            doc = json.loads(Path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"{name}: cannot read a JSON document")
            failed = True
            continue
        errors = validate(doc, schema)
        for error in errors:
            print(f"{name}: {error}")
        if errors:
            failed = True
        else:
            print(f"ok {name}")
    return 2 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger.py", description="Sentinel ops ledger")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("append", help="append one event described by OPS_EVENT_* env vars")
    validate_cmd = commands.add_parser("validate", help="validate event files")
    validate_cmd.add_argument("files", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "append":
        return _cmd_append()
    return _cmd_validate(args.files)


if __name__ == "__main__":
    sys.exit(main())
