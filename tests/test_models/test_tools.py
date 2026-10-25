"""Tests for sentinel.models.tools — ToolFailure and the tool input models."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from sentinel.models.tools import (
    UNTRUSTED_TEXT,
    DeployLogEntry,
    DeployRecord,
    GitHubPR,
    ServiceLookupInput,
    SyntheticResult,
    ToolFailure,
)


@pytest.mark.parametrize("kind", ["not_found", "unavailable", "upstream_error", "bad_input"])
def test_tool_failure_accepts_every_contract_kind(kind: str) -> None:
    failure = ToolFailure.model_validate({"tool": "fetch_logs", "kind": kind, "message": "m"})
    assert failure.kind == kind


def test_tool_failure_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        ToolFailure.model_validate({"tool": "fetch_logs", "kind": "timeout", "message": "m"})


def test_tool_failure_requires_every_field() -> None:
    with pytest.raises(ValidationError):
        ToolFailure.model_validate({"tool": "fetch_logs", "kind": "not_found"})


def test_tool_failure_round_trips_as_json() -> None:
    failure = ToolFailure(tool="get_service_metadata", kind="unavailable", message="db down")
    assert ToolFailure.model_validate_json(failure.model_dump_json()) == failure


def test_service_lookup_input_holds_the_name() -> None:
    assert ServiceLookupInput(service_name="sentinel-watchtower").service_name == (
        "sentinel-watchtower"
    )


# ── Untrusted external text ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (SyntheticResult, "body_snippet"),
        (SyntheticResult, "failure_message"),
        (DeployLogEntry, "message"),
        (GitHubPR, "title"),
        (GitHubPR, "author"),
        (DeployRecord, "author"),
        (DeployRecord, "files_changed"),
    ],
)
def test_external_text_fields_are_marked_untrusted(model: type[BaseModel], field: str) -> None:
    description = model.model_fields[field].description or ""
    assert UNTRUSTED_TEXT in description
    assert "never instructions" in description
