"""Tests for sentinel.providers.output_coercion."""

from __future__ import annotations

from pydantic import BaseModel

from sentinel.providers.output_coercion import _strip_fences, coerce_output


class _SimpleModel(BaseModel):
    name: str
    value: int


class _NestedModel(BaseModel):
    service: str
    severity: str
    score: float


# ── Happy-path coercion ───────────────────────────────────────────────────────


def test_coerce_plain_json() -> None:
    result = coerce_output('{"name": "test", "value": 42}', _SimpleModel)
    assert result is not None
    assert result.name == "test"
    assert result.value == 42


def test_coerce_json_fenced_with_language() -> None:
    text = '```json\n{"name": "fenced", "value": 99}\n```'
    result = coerce_output(text, _SimpleModel)
    assert result is not None
    assert result.name == "fenced"
    assert result.value == 99


def test_coerce_json_fenced_without_language() -> None:
    text = '```\n{"name": "plain-fence", "value": 7}\n```'
    result = coerce_output(text, _SimpleModel)
    assert result is not None
    assert result.name == "plain-fence"


def test_coerce_nested_model() -> None:
    text = '{"service": "api-gateway", "severity": "P1", "score": 4.5}'
    result = coerce_output(text, _NestedModel)
    assert result is not None
    assert result.service == "api-gateway"
    assert result.severity == "P1"
    assert result.score == 4.5


# ── Failure / None cases ──────────────────────────────────────────────────────


def test_coerce_returns_none_on_plain_text() -> None:
    result = coerce_output("I could not determine the root cause.", _SimpleModel)
    assert result is None


def test_coerce_returns_none_on_empty_string() -> None:
    result = coerce_output("", _SimpleModel)
    assert result is None


def test_coerce_returns_none_on_invalid_json() -> None:
    result = coerce_output("{not: valid json}", _SimpleModel)
    assert result is None


def test_coerce_returns_none_on_missing_required_field() -> None:
    result = coerce_output('{"name": "missing_value_field"}', _SimpleModel)
    assert result is None


def test_coerce_returns_none_on_wrong_type() -> None:
    result = coerce_output('{"name": 123, "value": "not-an-int"}', _SimpleModel)
    assert result is None


# ── _strip_fences unit tests ──────────────────────────────────────────────────


def test_strip_fences_json_block() -> None:
    text = '```json\n{"key": "val"}\n```'
    assert _strip_fences(text) == '{"key": "val"}'


def test_strip_fences_plain_block() -> None:
    text = '```\n{"key": "val"}\n```'
    assert _strip_fences(text) == '{"key": "val"}'


def test_strip_fences_no_fence() -> None:
    text = '{"key": "val"}'
    assert _strip_fences(text) == text


def test_strip_fences_multiline_content() -> None:
    text = '```json\n{\n  "a": 1,\n  "b": 2\n}\n```'
    assert _strip_fences(text) == '{\n  "a": 1,\n  "b": 2\n}'


def test_strip_fences_preserves_inner_whitespace() -> None:
    text = "```json\n  spaced content  \n```"
    assert _strip_fences(text) == "spaced content"
