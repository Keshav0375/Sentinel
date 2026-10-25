"""scripts/ops/ledger.py: identity, allow-list, secret scan, text cap, validator, CLI (task 10.1).

Fake secrets are assembled at runtime so gitleaks and the CI secret grep never see a literal.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = REPO_ROOT / "scripts/ops/ledger.py"
FIXTURES = REPO_ROOT / "dashboard-web/src/fixtures"

MakeEvent = Callable[..., dict[str, Any]]

CORRELATION = "6f9619ff-8b86-4d11-b42d-00c04fc964ff"
INCIDENT_SOURCE = "gha:Keshav0375/Sentinel/incident_response.yml"

RUN = {"version", "duration_s", "run_url"}
RUN_FAILED = RUN | {"failed_stage"}
EXPECTED_DATA_FIELDS: dict[str, set[str]] = {
    "build.started": RUN,
    "build.succeeded": RUN,
    "build.failed": RUN_FAILED,
    "deploy.started": RUN,
    "deploy.succeeded": RUN,
    "deploy.failed": RUN_FAILED,
    "ci.pr_gate.completed": {"conclusion", "duration_s", "run_url"},
    "scale.changed": {"replicas", "keep_warm", "duration_s", "run_url"},
    "alert.received": {"signal_type", "severity", "service", "monitor_url"},
    "agent.started": {"run_url"},
    "agent.decided": {
        "resolution_type",
        "confidence",
        "root_cause_summary",
        "tool_calls",
        "reflexion_loops",
        "mttr_s",
        "models_used",
        "langfuse_trace_url",
        "run_url",
    },
    "agent.failed": {"error_class", "langfuse_trace_url", "run_url"},
    "revert_pr.opened": {"url", "state"},
    "revert_pr.review_submitted": {"url", "reviewer", "review_state", "waited_s"},
    "revert_pr.merged": {"url", "state", "reviewer", "waited_s"},
    "revert_pr.closed": {"url", "state", "waited_s"},
    "escalation.raised": {"summary", "langfuse_trace_url", "run_url"},
    "eval.scored": {
        "case",
        "checks",
        "judge",
        "total_score",
        "judge_model",
        "judge_conflict",
        "judge_failed",
        "langfuse_trace_url",
        "run_url",
    },
    "trace.summarized": {"trace_id", "langfuse_trace_url", "total_duration_ms", "spans"},
}

# Runtime-built; together they hit all 16 SECRET_PATTERNS.
SECRET_SAMPLES = [
    "gh" + "p_" + "A" * 36,
    "github" + "_pat_" + "B" * 30,
    "sk" + "-ant-" + "C" * 24,
    "sk" + "-" + "D" * 24,
    "pk" + "-lf-" + "E" * 12,
    "AK" + "IA" + "F" * 16,
    "ey" + "J" + "a" * 12 + "." + "b" * 12 + ".c",
    "-----BEGIN " + "RSA PRIVATE KEY",
    "xo" + "xb-" + "1" * 12,
    "postgres" + "://u:p" + "w@h/db",
    "Account" + "Key=abc",
    "abc" + "8Q~" + "g" * 32,
    "Bearer " + "h" * 20,
    "pass" + "word=hunter22",
    "https://x.webhook" + ".office.com/abc",
    "10.0." + "0.12",
    "fe80:0:0:0:0:0:0:1",
]

BENIGN_SAMPLES = [
    "sha-0a1b2c3",
    "pr-55-2c8f0e7",
    "claude-sonnet-4-5 and gpt-4.1",
    "risk-assessment-for-the-orders-service-endpoint",
    "4f1d2a9b0c3e5f61728394a5b6c7d8e9f0a1b2c3",
    "version 1.2.3",
]


def _decided(make_event: MakeEvent, summary: str) -> dict[str, Any]:
    return make_event(
        "agent.decided",
        source=INCIDENT_SOURCE,
        source_id=CORRELATION,
        state="rollback",
        chain={"correlation_id": CORRELATION},
        data={"resolution_type": "rollback", "confidence": 0.5, "root_cause_summary": summary},
    )


def _span(**overrides: Any) -> dict[str, Any]:
    span: dict[str, Any] = {
        "span_id": "s1",
        "parent_id": None,
        "name": "incident_pipeline",
        "kind": "AGENT",
        "start_ms": 0,
        "duration_ms": 10,
        "status": "ok",
    }
    span.update(overrides)
    return span


def _trace(make_event: MakeEvent, *spans: dict[str, Any]) -> dict[str, Any]:
    return make_event(
        "trace.summarized",
        source="langfuse",
        source_id="tr1",
        chain={"correlation_id": CORRELATION},
        data={"trace_id": "tr1", "spans": list(spans)},
    )


def _data_def(schema: dict[str, Any], kind: str) -> dict[str, Any]:
    for branch in schema["allOf"]:
        if branch["if"]["properties"]["kind"]["const"] == kind:
            ref = branch["then"]["properties"]["data"]["$ref"]
            return schema["$defs"][ref.rsplit("/", 1)[1]]
    raise AssertionError(kind)


# ── event_id ──────────────────────────────────────────────────────────────────


def test_event_id_is_deterministic(ledger: ModuleType) -> None:
    args = ("deploy.failed", "gha:Keshav0375/Sentinel/x.yml", "42.1")
    first = ledger.compute_event_id(*args)
    assert first == ledger.compute_event_id(*args)
    assert (
        first == hashlib.sha256(b"deploy.failed\ngha:Keshav0375/Sentinel/x.yml\n42.1\n").hexdigest()
    )
    assert re.fullmatch(r"[0-9a-f]{64}", first)


@pytest.mark.parametrize("index", range(4))
def test_event_id_changes_with_each_part(ledger: ModuleType, index: int) -> None:
    parts = ["deploy.failed", "gha:Keshav0375/Sentinel/x.yml", "42.1", "s"]
    changed = list(parts)
    changed[index] += "x"
    assert ledger.compute_event_id(*parts) != ledger.compute_event_id(*changed)


def test_event_id_rejects_line_breaks(ledger: ModuleType) -> None:
    with pytest.raises(ledger.LedgerError):
        ledger.compute_event_id("deploy.failed", "gha:Keshav0375/Sentinel/x.yml", "a\nb")


def test_event_id_ignores_ts_recorded_at_and_data(make_event: MakeEvent) -> None:
    first = make_event()
    second = make_event(
        ts="2026-10-26T08:00:00Z",
        recorded_at=datetime(2026, 10, 26, 8, 0, 1, tzinfo=UTC),
        data={
            "run_url": "https://github.com/Keshav0375/Sentinel-deployment/actions/runs/18800000202",
            "version": "pr-61-4f1d2a9",
        },
    )
    assert first["ts"] != second["ts"]
    assert first["event_id"] == second["event_id"]


# ── envelope ──────────────────────────────────────────────────────────────────


def test_build_event_envelope(make_event: MakeEvent) -> None:
    event = make_event(ts="2026-10-25T14:00:00+02:00")
    assert list(event) == [
        "schema",
        "event_id",
        "kind",
        "ts",
        "recorded_at",
        "source",
        "chain",
        "data",
    ]
    assert event["schema"] == "ops.v1"
    assert event["ts"] == "2026-10-25T12:00:00Z"
    assert event["recorded_at"] == "2026-10-25T12:00:05Z"


@pytest.mark.parametrize("ts", ["yesterday", "2026-10-25T12:00:00", datetime(2026, 10, 25)])
def test_ts_rejected(ledger: ModuleType, make_event: MakeEvent, ts: Any) -> None:
    with pytest.raises(ledger.LedgerError):
        make_event(ts=ts)


# ── allow-list ────────────────────────────────────────────────────────────────


def test_unknown_data_fields_dropped_with_warning(make_event: MakeEvent) -> None:
    messages: list[str] = []
    event = make_event(
        data={
            "run_url": "https://github.com/Keshav0375/Sentinel-deployment/actions/runs/18800000202",
            "log": "boom",
            "stdout": "x",
        },
        warn=messages.append,
    )
    assert "log" not in event["data"]
    assert "stdout" not in event["data"]
    assert len(messages) == 1
    assert "data.log" in messages[0]
    assert "data.stdout" in messages[0]
    assert "boom" not in messages[0]


def test_unknown_chain_fields_dropped(make_event: MakeEvent) -> None:
    event = make_event(chain={"sha": "4f1d2a9", "branch": "main"})
    assert event["chain"] == {"sha": "4f1d2a9"}


def test_none_values_dropped(make_event: MakeEvent) -> None:
    event = make_event(
        data={
            "version": None,
            "run_url": "https://github.com/Keshav0375/Sentinel-deployment/actions/runs/18800000202",
        }
    )
    assert "version" not in event["data"]


def test_allow_list_is_frozen(ledger: ModuleType) -> None:
    kinds = ledger.load_schema()["properties"]["kind"]["enum"]
    assert set(kinds) == set(EXPECTED_DATA_FIELDS)
    for kind in kinds:
        assert ledger.allowed_data_fields(kind) == EXPECTED_DATA_FIELDS[kind], kind
    assert ledger.allowed_chain_fields() == {
        "correlation_id",
        "sha",
        "pr",
        "run_id",
        "incident_id",
        "scenario",
    }


def test_failed_stage_stripped_on_success_kinds(make_event: MakeEvent) -> None:
    event = make_event("deploy.succeeded")
    assert "failed_stage" not in event["data"]
    assert event["data"]["run_url"].endswith("/18800000202")


def test_unknown_kind_rejected(ledger: ModuleType, make_event: MakeEvent) -> None:
    with pytest.raises(ledger.SchemaValidationError):
        make_event("deploy.exploded")


# ── secrets ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sample", SECRET_SAMPLES)
def test_secret_values_fail_closed(ledger: ModuleType, make_event: MakeEvent, sample: str) -> None:
    with pytest.raises(ledger.SecretDetectedError) as caught:
        _decided(make_event, f"the agent saw {sample} in the logs")
    assert "data.root_cause_summary" in str(caught.value)
    assert sample not in str(caught.value)


def test_secret_samples_cover_every_pattern(ledger: ModuleType) -> None:
    hit = {name for name, rx in ledger.SECRET_PATTERNS for s in SECRET_SAMPLES if rx.search(s)}
    assert hit == {name for name, _ in ledger.SECRET_PATTERNS}
    assert len(ledger.SECRET_PATTERNS) == 16


def test_secret_in_nested_span_rejected(ledger: ModuleType, make_event: MakeEvent) -> None:
    with pytest.raises(ledger.SecretDetectedError) as caught:
        _trace(make_event, _span(name="gh" + "p_" + "A" * 36))
    assert caught.value.paths == ["data.spans[0].name"]


@pytest.mark.parametrize("sample", BENIGN_SAMPLES)
def test_benign_values_pass(make_event: MakeEvent, sample: str) -> None:
    event = _decided(make_event, sample)
    assert event["data"]["root_cause_summary"] == sample


# ── R14 text cap ──────────────────────────────────────────────────────────────


def test_summary_capped_at_500(make_event: MakeEvent) -> None:
    capped = _decided(make_event, "a" * 800)["data"]["root_cause_summary"]
    assert len(capped) == 500
    assert capped.endswith("…")
    assert capped[:499] == "a" * 499
    assert _decided(make_event, "b" * 500)["data"]["root_cause_summary"] == "b" * 500

    escalation = make_event(
        "escalation.raised",
        source=INCIDENT_SOURCE,
        source_id=CORRELATION,
        chain={"correlation_id": CORRELATION},
        data={"summary": "c" * 800},
    )
    assert len(escalation["data"]["summary"]) == 500
    assert escalation["data"]["summary"].endswith("…")


def test_cap_matches_schema(ledger: ModuleType, event_schema: dict[str, Any]) -> None:
    assert event_schema["$defs"]["summary"]["maxLength"] == ledger.TEXT_CAP == 500
    for kind, fields in ledger.CAPPED_TEXT.items():
        props = _data_def(event_schema, kind)["properties"]
        for field in fields:
            assert props[field] == {"$ref": "#/$defs/summary"}, (kind, field)


def test_other_long_strings_rejected(ledger: ModuleType, make_event: MakeEvent) -> None:
    with pytest.raises(ledger.SchemaValidationError) as caught:
        _trace(make_event, _span(name="n" * 121))
    assert any(e.startswith("/data/spans/0/name: too long") for e in caught.value.errors)


def test_errors_never_echo_values(ledger: ModuleType, make_event: MakeEvent) -> None:
    with pytest.raises(ledger.SchemaValidationError) as caught:
        make_event(data={"run_url": "https://evil.example/x"})
    assert "/data/run_url" in caught.value.errors[0]
    assert "evil" not in str(caught.value)


# ── validator ─────────────────────────────────────────────────────────────────


def test_validator_types(ledger: ModuleType, make_event: MakeEvent) -> None:
    schema = ledger.load_schema()
    scale = make_event(
        "scale.changed",
        state="1",
        data={
            "replicas": 1,
            "run_url": "https://github.com/Keshav0375/Sentinel/actions/runs/1",
        },
    )
    assert ledger.validate(scale, schema) == []
    as_bool = copy.deepcopy(scale)
    as_bool["data"]["replicas"] = True
    assert ledger.validate(as_bool, schema) == ["/data/replicas: expected integer"]
    as_float = copy.deepcopy(scale)
    as_float["data"]["replicas"] = 1.0
    assert ledger.validate(as_float, schema) == []

    scored = make_event(
        "eval.scored",
        state="pass/01",
        chain={"scenario": "pass/01"},
        data={"judge_conflict": False, "judge_failed": False},
    )
    assert ledger.validate(scored, schema) == []
    scored["data"]["judge_failed"] = 0
    assert ledger.validate(scored, schema) == ["/data/judge_failed: expected boolean"]


@pytest.mark.parametrize(
    "schema",
    [{"type": "string", "format": "date-time"}, {"else": {}}, {"$ref": "other.json"}],
)
def test_check_schema_rejects_unsupported(ledger: ModuleType, schema: dict[str, Any]) -> None:
    with pytest.raises(ledger.SchemaError):
        ledger.check_schema(schema)


def _entity_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in sorted((FIXTURES / "populated" / "entities").glob("*.json")):
        events += json.loads(path.read_text())["events"]
    return events


def _mutations(base: dict[str, Any]) -> list[dict[str, Any]]:
    def mutate(fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        event = copy.deepcopy(base)
        fn(event)
        return event

    def succeeded_with_stage(e: dict[str, Any]) -> None:
        e["kind"] = "deploy.succeeded"
        e["data"]["failed_stage"] = "verify"

    return [
        mutate(lambda e: e["data"].update(log="x")),
        mutate(lambda e: e["chain"].update(branch="main")),
        mutate(lambda e: e.update(source="gha:evil/repo/x.yml")),
        mutate(lambda e: e.update(ts="2026-10-25T12:00:00.5Z")),
        mutate(
            lambda e: e.update(
                kind="agent.decided", data={"resolution_type": "rollback", "confidence": 1.5}
            )
        ),
        mutate(lambda e: e.update(kind="deploy.exploded")),
        mutate(lambda e: e.pop("data")),
        mutate(succeeded_with_stage),
    ]


def test_validator_agrees_with_jsonschema(
    ledger: ModuleType,
    event_schema: dict[str, Any],
    snapshot_schema: dict[str, Any],
    make_event: MakeEvent,
) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    events = _entity_events()
    assert len(events) == 29
    mutations = _mutations(make_event())
    reference = jsonschema.Draft202012Validator(event_schema)
    for event in [*events, *mutations]:
        assert (ledger.validate(event, event_schema) == []) == reference.is_valid(event), event
    assert all(ledger.validate(m, event_schema) for m in mutations)

    snapshot_reference = jsonschema.Draft202012Validator(snapshot_schema)
    for path in FIXTURES.rglob("*.json"):
        doc = json.loads(path.read_text())
        ours = ledger.validate(doc, snapshot_schema) == []
        assert ours == snapshot_reference.is_valid(doc), path


# ── path and bytes ────────────────────────────────────────────────────────────


def test_event_path_layout(ledger: ModuleType, make_event: MakeEvent) -> None:
    event = make_event()
    eid = event["event_id"]
    assert (
        ledger.event_path(event) == f"events/2026/10/25/20261025T120000Z-deploy.failed-{eid}.json"
    )


def test_serialize_round_trip(ledger: ModuleType, make_event: MakeEvent) -> None:
    event = make_event()
    raw = ledger.serialize(event)
    assert raw.endswith(b"}\n")
    loaded = json.loads(raw)
    assert loaded == event
    assert list(loaded) == list(event)


# ── CLI ───────────────────────────────────────────────────────────────────────


def _cli_env(base: dict[str, str], remote_uri: str, **overrides: str) -> dict[str, str]:
    env = {k: v for k, v in base.items() if not k.startswith("OPS_")}
    env.update(
        {
            "OPS_EVENT_KIND": "deploy.failed",
            "OPS_EVENT_SOURCE": "gha:Keshav0375/Sentinel-deployment/deploy.yml",
            "OPS_EVENT_SOURCE_ID": "18800000202.1",
            "OPS_EVENT_TS": "2026-10-25T12:00:00Z",
            "OPS_EVENT_CHAIN": '{"sha": "4f1d2a9"}',
            "OPS_EVENT_DATA": json.dumps(
                {
                    "run_url": "https://github.com/Keshav0375/Sentinel-deployment/actions/runs/1",
                    "failed_stage": "verify",
                }
            ),
            "OPS_LEDGER_REMOTE": remote_uri,
        }
    )
    env.update(overrides)
    return {k: v for k, v in env.items() if v != "<unset>"}


def _cli(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LEDGER_PATH), "append"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_cli_append_end_to_end(remote: Any, env_without_git: dict[str, str]) -> None:
    env = _cli_env(env_without_git, remote.uri)
    first = _cli(env)
    assert first.returncode == 0, first.stderr
    lines = first.stdout.splitlines()
    assert len(lines) == 2
    assert re.fullmatch(r"event-id=[0-9a-f]{64}", lines[0])
    assert lines[1] == "result=written"
    assert len(remote.events()) == 1

    again = _cli(env)
    assert again.returncode == 0, again.stderr
    assert again.stdout.splitlines() == [lines[0], "result=exists"]
    assert len(remote.events()) == 1


@pytest.mark.parametrize(
    ("overrides", "secret"),
    [
        ({"OPS_EVENT_KIND": "<unset>"}, None),
        ({"OPS_EVENT_DATA": "[1]"}, None),
        ({"OPS_EVENT_DATA": "{bad"}, None),
        (
            {
                "OPS_EVENT_DATA": json.dumps(
                    {
                        "run_url": "https://github.com/Keshav0375/Sentinel/actions/runs/1",
                        "version": "gh" + "p_" + "Z" * 36,
                    }
                )
            },
            "gh" + "p_" + "Z" * 36,
        ),
    ],
)
def test_cli_input_errors_exit_2(
    tmp_path: Path,
    env_without_git: dict[str, str],
    overrides: dict[str, str],
    secret: str | None,
) -> None:
    # Failures happen before git; the remote is local and absent anyway.
    proc = _cli(_cli_env(env_without_git, (tmp_path / "absent.git").as_uri(), **overrides))
    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.startswith("ops-ledger:")
    assert proc.stdout == ""
    if secret is not None:
        assert secret not in proc.stderr
        assert "data.version" in proc.stderr


def test_main_push_failure_exits_3(
    ledger: ModuleType,
    tmp_path: Path,
    git_bin: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = _cli_env({}, (tmp_path / "missing.git").as_uri())
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OPS_LEDGER_TOKEN", raising=False)
    monkeypatch.delenv("OPS_EVENT_STATE", raising=False)
    sleeps: list[float] = []
    monkeypatch.setattr(ledger, "_sleep", sleeps.append)
    assert ledger.main(["append"]) == 3
    assert len(sleeps) == ledger.MAX_PUSH_ATTEMPTS - 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "failed after 5 attempts" in captured.err


def test_cli_validate_reports_each_file(
    ledger: ModuleType,
    make_event: MakeEvent,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    good = tmp_path / "good.json"
    good.write_bytes(ledger.serialize(make_event()))
    bad = tmp_path / "bad.json"
    broken = make_event()
    broken["data"]["log"] = "x"
    bad.write_text(json.dumps(broken))
    assert ledger.main(["validate", str(good)]) == 0
    assert ledger.main(["validate", str(good), str(bad)]) == 2
    out = capsys.readouterr().out
    assert f"ok {good}" in out
    assert f"{bad}: /data: unexpected field log" in out
