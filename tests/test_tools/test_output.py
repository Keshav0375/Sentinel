"""Tests for sentinel.tools.output — the compact JSON every tool returns to the model."""

from __future__ import annotations

import json

from sentinel.models.tools import EscalationContext, GitHubPR, ToolFailure
from sentinel.tools.output import tool_json


def test_a_model_is_compact_json_without_none_fields() -> None:
    pr = GitHubPR(title="Fix /health", html_url="https://github.com/o/r/pull/5")
    text = tool_json(pr)
    assert text == '{"title":"Fix /health","html_url":"https://github.com/o/r/pull/5"}'
    assert "None" not in text and "null" not in text


def test_a_list_is_a_json_array() -> None:
    items = [
        EscalationContext(hypothesis="h1", evidence=["e"], confidence=0.2, missing=[]),
        EscalationContext(hypothesis="h2", evidence=[], confidence=0.4, missing=["m"]),
    ]
    data = json.loads(tool_json(items))
    assert [item["hypothesis"] for item in data] == ["h1", "h2"]


def test_an_empty_list_is_an_empty_array() -> None:
    assert tool_json([]) == "[]"


def test_a_failure_carries_the_error_discriminator() -> None:
    failure = ToolFailure(tool="fetch_logs", kind="unavailable", message="no keys")
    data = json.loads(tool_json(failure))
    assert data == {
        "error": True,
        "tool": "fetch_logs",
        "kind": "unavailable",
        "message": "no keys",
    }
    assert next(iter(data)) == "error"  # first thing the model reads


def test_a_failure_round_trips() -> None:
    failure = ToolFailure(tool="t", kind="bad_input", message="m")
    assert ToolFailure.model_validate_json(tool_json(failure)) == failure
