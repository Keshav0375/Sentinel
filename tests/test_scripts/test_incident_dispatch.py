"""Tests for scripts/incident_dispatch.py — the data shaping of the incident response workflow."""

from __future__ import annotations

import importlib.util
import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sentinel.models.alert import IncidentPayload

# scripts/ is not on the test Python path — load via importlib.
_SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "incident_dispatch.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("incident_dispatch_script", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_script: Any = _load_script()

_CORRELATION = "6f9619ff-8b86-4d11-b42d-00c04fc964ff"
_SHA40 = "a" * 40
_OUTPUT_LINE = re.compile(r"^[a-z_]+=[^\n]*$")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _cp(**kw: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "signal_type": "deploy_failure",
        "correlation_id": _CORRELATION,
        "event": {
            "title": "Deploy failed",
            "tags": "service:sentinel-watchtower,deploy_status:failed,version:pr-12,env:dev",
            "alert_id": "123",
        },
    }
    return payload | kw


@pytest.fixture
def event_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, Any]], Path]:
    def write(client_payload: dict[str, Any]) -> Path:
        path = tmp_path / "event.json"
        path.write_text(json.dumps({"action": "incident-alert", "client_payload": client_payload}))
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
        return path

    return write


def _pipeline_result(**kw: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "incident_id": "i-1",
        "status": "rollback_proposed",
        "resolution_type": "rollback",
        "root_cause": "bad deploy\nof pr-12",
        "confidence": 0.9,
        "target_deploy": "a1b2c3d",
        "evidence_summary": "5xx after deploy",
        "escalation": None,
    }
    return result | kw


def _incident(status: str = "rollback_proposed", **result_kw: Any) -> dict[str, Any]:
    return {"status": status, "trajectory": {"pipeline_result": _pipeline_result(**result_kw)}}


# ── correlation_uuid ──────────────────────────────────────────────────────────


def test_correlation_uuid_keeps_a_uuid() -> None:
    assert _script.correlation_uuid(_CORRELATION.upper()) == _CORRELATION
    assert _script.correlation_uuid("{" + _CORRELATION + "}") == _CORRELATION


def test_correlation_uuid_maps_a_non_uuid_deterministically() -> None:
    expected = str(uuid.uuid5(uuid.NAMESPACE_URL, "sentinel:evt-42"))

    assert _script.correlation_uuid("evt-42") == expected
    assert _script.correlation_uuid("evt-42") == expected
    assert _script.correlation_uuid("evt-43") != expected


@pytest.mark.parametrize("raw", [None, "", "  ", 42])
def test_correlation_uuid_rejects_missing(raw: object) -> None:
    with pytest.raises(_script.MappingError):
        _script.correlation_uuid(raw)


# ── tags ──────────────────────────────────────────────────────────────────────


def test_normalise_tags_string() -> None:
    tags = _script.normalise_tags(
        "service:a, version:pr-1:x,env:dev,failed_stage:,deploy_status:failed,service:b,junk"
    )

    assert tags == {"service": "b", "version": "pr-1:x", "deploy_status": "failed"}


def test_normalise_tags_list() -> None:
    tags = _script.normalise_tags(["service:a", "env:dev", 3, "version:pr-2"])

    assert tags == {"service": "a", "version": "pr-2"}


def test_normalise_tags_dict() -> None:
    tags = _script.normalise_tags(
        {"service": " a ", "version": "", "env": "dev", "failed_stage": 5, "deploy_status": "ok"}
    )

    assert tags == {"service": "a", "deploy_status": "ok"}


@pytest.mark.parametrize("raw", [None, 42, 1.5])
def test_normalise_tags_none_and_other(raw: object) -> None:
    assert _script.normalise_tags(raw) == {}


def test_service_of_and_pr_number_of() -> None:
    assert _script.service_of({"service": "sentinel-watchtower"}) == "sentinel-watchtower"
    assert _script.service_of({"service": "a b"}) == ""
    assert _script.service_of({"service": "$(x)"}) == ""
    assert _script.service_of({}) == ""
    assert _script.pr_number_of({"version": "pr-12"}) == "12"
    assert _script.pr_number_of({"version": "v1.2"}) == ""
    assert _script.pr_number_of({"version": "pr-x"}) == ""
    assert _script.pr_number_of({}) == ""


# ── build_payload ─────────────────────────────────────────────────────────────


def test_build_payload_maps_the_dispatch() -> None:
    cp = _cp(
        event={"title": "  Deploy failed  ", "tags": "service:s,version:pr-12", "alert_id": "9"}
    )

    payload = _script.build_payload(
        cp,
        service_metadata={"tier": "critical"},
        pr_details={"number": 12},
        recent_logs=[{"message": "boom"}],
    )

    assert payload["title"] == "Deploy failed"
    assert payload["source"] == "datadog"
    assert payload["tags"] == {"service": "s", "version": "pr-12"}
    assert payload["alert_id"] == "9"
    assert payload["context"] == {
        "service_metadata": {"tier": "critical"},
        "pr_details": {"number": 12},
        "recent_logs": [{"message": "boom"}],
    }
    assert "severity" not in payload and "raw_payload" not in payload
    model = IncidentPayload.model_validate(payload)
    assert model.signal_type == "deploy_failure"
    assert model.correlation_id == _CORRELATION
    assert model.tags.version == "pr-12"


def test_build_payload_maps_severity_and_dd_event_id() -> None:
    cp = _cp(event={"title": "t", "severity": " error ", "dd_event_id": "7613449929768408044"})

    payload = _script.build_payload(cp, service_metadata=None, pr_details=None, recent_logs=None)

    assert payload["severity"] == "error"
    assert payload["dd_event_id"] == "7613449929768408044"
    model = IncidentPayload.model_validate(payload)
    assert model.severity == "error"
    assert model.dd_event_id == "7613449929768408044"


def test_build_payload_omits_blank_severity_and_dd_event_id() -> None:
    cp = _cp(event={"title": "t", "severity": "  ", "dd_event_id": 123})

    payload = _script.build_payload(cp, service_metadata=None, pr_details=None, recent_logs=None)

    assert "severity" not in payload
    assert "dd_event_id" not in payload


def test_build_payload_omits_context_of_the_wrong_type() -> None:
    payload = _script.build_payload(
        _cp(event={"title": "t"}), service_metadata="str", pr_details=[1], recent_logs={"a": 1}
    )

    assert payload["context"] == {}
    assert "alert_id" not in payload
    assert payload["tags"] == {}


@pytest.mark.parametrize(
    "client_payload",
    [
        _cp(signal_type="metric_alert"),
        _cp(signal_type=None),
        _cp(event="text"),
        _cp(event={"title": "   "}),
        _cp(event={"tags": "service:s"}),
        _cp(correlation_id=""),
    ],
)
def test_build_payload_rejects(client_payload: dict[str, Any]) -> None:
    with pytest.raises(_script.MappingError):
        _script.build_payload(client_payload)


# ── shape_pr_details / shape_logs ─────────────────────────────────────────────


def test_shape_pr_details_uses_the_merge_commit() -> None:
    pull = {
        "number": 12,
        "merged": True,
        "merge_commit_sha": _SHA40,
        "head": {"sha": "b" * 40},
        "user": {"login": "dev"},
    }
    files = [f"f{i}.py" for i in range(150)] + [""]

    shaped = _script.shape_pr_details(pull, files)

    assert shaped["sha"] == _SHA40
    assert shaped["number"] == 12 and shaped["author"] == "dev"
    assert len(shaped["files_changed"]) == _script.MAX_FILES == 100
    assert _script.shape_pr_details(pull | {"merged": False}, files) is None
    assert _script.shape_pr_details(pull | {"merge_commit_sha": "abc"}, files) is None
    assert _script.shape_pr_details(pull | {"merge_commit_sha": None}, files) is None


def test_shape_logs_truncates() -> None:
    entry = {
        "attributes": {
            "timestamp": "2026-10-24T00:00:00Z",
            "status": "error",
            "service": "s",
            "message": "x" * 900,
            "extra": "dropped",
        }
    }

    logs = _script.shape_logs({"data": [entry] * 60})

    assert len(logs) == 50
    assert len(logs[0]["message"]) == 500 + len(_script.TRUNCATED)
    assert set(logs[0]) == {"timestamp", "status", "service", "message"}
    assert _script.shape_logs({}) == []
    assert _script.shape_logs({"data": [{}]}) == [
        {"timestamp": "", "status": "", "service": "", "message": ""}
    ]


# ── result_outputs ────────────────────────────────────────────────────────────


def test_result_outputs_rollback() -> None:
    out = _script.result_outputs(_incident())

    assert list(out) == [
        "resolution_type",
        "root_cause",
        "confidence",
        "target_deploy",
        "evidence_summary",
        "missing",
    ]
    assert out["resolution_type"] == "rollback"
    assert out["target_deploy"] == "a1b2c3d"
    assert out["confidence"] == "0.90"
    assert out["root_cause"] == "bad deploy of pr-12"
    assert out["evidence_summary"] == "5xx after deploy"
    assert out["missing"] == ""


def test_result_outputs_escalated() -> None:
    incident = _incident(
        "escalated",
        resolution_type="escalated",
        target_deploy=None,
        confidence=0.4,
        escalation={"hypothesis": "h", "evidence": [], "confidence": 0.4, "missing": ["a", "b"]},
    )

    out = _script.result_outputs(incident)

    assert out["resolution_type"] == "escalated"
    assert out["target_deploy"] == ""
    assert out["missing"] == "a; b"
    assert out["confidence"] == "0.40"


@pytest.mark.parametrize("incident", [None, {"status": "in_progress"}, {"status": "triage"}])
def test_result_outputs_timeout(incident: dict[str, Any] | None) -> None:
    out = _script.result_outputs(incident)

    assert out["resolution_type"] == "escalated"
    assert out["evidence_summary"] == "pipeline timeout"
    assert out["confidence"] == "0"


@pytest.mark.parametrize("trajectory", [{}, None, {"pipeline_result": []}])
def test_result_outputs_missing_pipeline_result(trajectory: Any) -> None:
    out = _script.result_outputs({"status": "rollback_proposed", "trajectory": trajectory})

    assert out["resolution_type"] == "escalated"
    assert out["evidence_summary"] == "pipeline result missing"


def test_result_outputs_rollback_status_mismatch() -> None:
    out = _script.result_outputs(_incident("escalated"))

    assert out["resolution_type"] == "escalated"
    assert out["target_deploy"] == ""


def test_result_outputs_drops_a_malformed_target_and_confidence() -> None:
    out = _script.result_outputs(_incident(target_deploy="not hex; rm -rf", confidence="n/a"))

    assert out["target_deploy"] == ""
    assert out["confidence"] == "0"


# ── summary_outputs ───────────────────────────────────────────────────────────

_ARGS: dict[str, str] = {
    "ensure_result": "success",
    "pipeline_result": "success",
    "create_result": "success",
    "record_result": "success",
    "resolution_type": "rollback",
    "pr_url": "https://github.com/Keshav0375/Sentinel-deployment/pull/7",
    "evidence": "5xx",
    "service": "sentinel-watchtower",
    "incident_id": "inc-1",
    "run_url": "https://github.com/x/y/actions/runs/1",
}


@pytest.mark.parametrize(
    ("overrides", "resolution", "severity", "alert_type", "outcome"),
    [
        ({"ensure_result": "failure"}, "failed", "critical", "error", "backend did not come up"),
        ({"pipeline_result": "failure"}, "failed", "critical", "error", "job ended failure"),
        ({}, "rollback", "warning", "warning", "revert PR opened for review: https://"),
        (
            {"create_result": "failure", "pr_url": ""},
            "failed",
            "critical",
            "error",
            "revert PR was not opened (failure)",
        ),
        (
            {"create_result": "", "pr_url": ""},
            "failed",
            "critical",
            "error",
            "revert PR was not opened (skipped)",
        ),
        # The summary keys on the PR url, not on the create job result.
        (
            {"create_result": "failure"},
            "rollback",
            "warning",
            "warning",
            "revert PR opened for review: https://",
        ),
        (
            {"record_result": "failure"},
            "rollback",
            "warning",
            "warning",
            "not recorded on the incident (failure)",
        ),
        (
            {"resolution_type": "escalated", "create_result": "skipped"},
            "escalated",
            "warning",
            "warning",
            "escalated: needs human review",
        ),
        (
            {"resolution_type": "escalated", "evidence": "pipeline timeout"},
            "escalated",
            "warning",
            "warning",
            "escalated: pipeline timeout",
        ),
    ],
)
def test_summary_outputs_cases(
    overrides: dict[str, str], resolution: str, severity: str, alert_type: str, outcome: str
) -> None:
    outputs, event = _script.summary_outputs(**(_ARGS | overrides))

    assert outputs["resolution"] == resolution
    assert outputs["severity"] == severity
    assert outcome in outputs["outcome"]
    assert event["alert_type"] == alert_type
    assert "source:sentinel" in event["tags"]
    assert f"resolution:{resolution}" in event["tags"]
    assert "service:sentinel-watchtower" in event["tags"]
    assert not any(tag.startswith("deploy_status") for tag in event["tags"])
    assert "run: https://github.com/x/y/actions/runs/1" in event["text"]


def test_summary_outputs_warning_only_when_record_failed() -> None:
    outputs, _ = _script.summary_outputs(**_ARGS)
    assert outputs["warning"] == ""

    outputs, event = _script.summary_outputs(**(_ARGS | {"record_result": ""}))
    assert "skipped" in outputs["warning"]
    assert outputs["warning"] in outputs["outcome"]
    assert outputs["warning"] in event["text"]

    outputs, _ = _script.summary_outputs(
        **(_ARGS | {"resolution_type": "escalated", "record_result": "failure"})
    )
    assert outputs["warning"] == ""


def test_summary_outputs_without_service_or_incident() -> None:
    _, event = _script.summary_outputs(**(_ARGS | {"service": "", "incident_id": ""}))

    assert not any(tag.startswith("service:") for tag in event["tags"])
    assert "incident: none" in event["text"]


# ── main ──────────────────────────────────────────────────────────────────────


def test_main_tags_prints_output_lines(
    event_file: Callable[[dict[str, Any]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    event_file(_cp())

    assert _script.main(["tags"]) == 0

    assert capsys.readouterr().out == "service=sentinel-watchtower\npr_number=12\n"


def test_main_tags_without_tags_prints_empty_values(
    event_file: Callable[[dict[str, Any]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    event_file(_cp(event={"title": "t"}))

    assert _script.main(["tags"]) == 0

    assert capsys.readouterr().out == "service=\npr_number=\n"


def test_main_payload_reads_env(
    event_file: Callable[[dict[str, Any]], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    event_file(_cp())
    monkeypatch.setenv("SERVICE_METADATA", '{"tier":"critical"}')
    monkeypatch.setenv("PR_DETAILS", "not json")
    monkeypatch.setenv("RECENT_LOGS", "[]")

    assert _script.main(["payload"]) == 0

    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    body = json.loads(captured.out)
    assert body["context"] == {"service_metadata": {"tier": "critical"}, "recent_logs": []}
    assert "pr_details" not in body["context"]
    assert "PR_DETAILS" in captured.err


def test_main_payload_error_exits_1(
    event_file: Callable[[dict[str, Any]], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    event_file(_cp(event={"title": " "}))
    for name in ("SERVICE_METADATA", "PR_DETAILS", "RECENT_LOGS"):
        monkeypatch.delenv(name, raising=False)

    assert _script.main(["payload"]) == 1

    assert "::error::incident_dispatch" in capsys.readouterr().err


def test_main_payload_without_an_event_file_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)

    assert _script.main(["payload"]) == 1

    assert "::error::incident_dispatch" in capsys.readouterr().err


def test_main_pr_details_and_logs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pull = tmp_path / "pull.json"
    pull.write_text(json.dumps({"number": 3, "merged": True, "merge_commit_sha": _SHA40}))
    files = tmp_path / "files.txt"
    files.write_text("a.py\nb.py\n")
    response = tmp_path / "dd.json"
    response.write_text(json.dumps({"data": [{"attributes": {"message": "m"}}]}))

    assert _script.main(["pr-details", "--pull", str(pull), "--files", str(files)]) == 0
    details = json.loads(capsys.readouterr().out)
    assert details["files_changed"] == ["a.py", "b.py"] and details["author"] is None

    pull.write_text(json.dumps({"number": 3, "merged": False}))
    assert _script.main(["pr-details", "--pull", str(pull), "--files", str(files)]) == 0
    assert capsys.readouterr().out == "null\n"

    assert _script.main(["logs", "--response", str(response)]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"timestamp": "", "status": "", "service": "", "message": "m"}
    ]


def test_main_result_and_summary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    incident = tmp_path / "incident.json"
    incident.write_text(json.dumps(_incident()))

    assert _script.main(["result", "--incident", str(incident)]) == 0
    assert "resolution_type=rollback\n" in capsys.readouterr().out

    assert _script.main(["result"]) == 0
    assert "evidence_summary=pipeline timeout\n" in capsys.readouterr().out

    for name, value in _ARGS.items():
        monkeypatch.setenv(name.upper(), value)
    event_path = tmp_path / "dd_event.json"
    assert _script.main(["summary", "--dd-event-file", str(event_path)]) == 0
    assert "resolution=rollback\n" in capsys.readouterr().out
    event = json.loads(event_path.read_text())
    assert event["title"] == "Sentinel incident rollback"
    assert "source:sentinel" in event["tags"]


def test_main_summary_prints_record_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in (_ARGS | {"record_result": "failure"}).items():
        monkeypatch.setenv(name.upper(), value)
    event_path = tmp_path / "dd_event.json"

    assert _script.main(["summary", "--dd-event-file", str(event_path)]) == 0

    captured = capsys.readouterr()
    assert any(line.startswith("warning=") for line in captured.out.splitlines())
    assert "::warning::incident_dispatch:" in captured.err


def test_every_output_value_is_single_line(
    event_file: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    event_file(_cp())
    incident = tmp_path / "incident.json"
    incident.write_text(
        json.dumps(
            _incident(root_cause="line one\nline two\r\nline three", evidence_summary="a\nb")
        )
    )
    for name, value in _ARGS.items():
        monkeypatch.setenv(name.upper(), value if name not in ("evidence", "pr_url") else "a\nb")
    event_path = tmp_path / "dd_event.json"

    assert _script.main(["tags"]) == 0
    assert _script.main(["result", "--incident", str(incident)]) == 0
    assert _script.main(["summary", "--dd-event-file", str(event_path)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines
    assert all(_OUTPUT_LINE.match(line) for line in lines), lines
