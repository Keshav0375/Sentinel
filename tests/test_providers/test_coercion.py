"""Tests for sentinel.providers.output_coercion."""

from __future__ import annotations

import pytest
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


# ── Framed replies (real models wrap their JSON) ──────────────────────────────

_OBJ = '{"name": "framed", "value": 5}'


@pytest.mark.parametrize(
    "text",
    [
        f"Here is my critique:\n```json\n{_OBJ}\n```",
        f"Here is my critique:\n```json{_OBJ}```",
        f"```json{_OBJ}```\n\nNote: confidence is limited by the missing logs.",
        f"```json\n{_OBJ}\n```\n\nNote: {{this}} is prose, not JSON.",
        f"Critique:\n{_OBJ}",
        f"Critique:\n{_OBJ}\nThat is all.",
        f"```\n{_OBJ}\n```",
        f'I said "hi {{" before.\n{_OBJ}',
    ],
    ids=[
        "prose-then-fence",
        "prose-then-inline-fence",
        "inline-fence-then-note",
        "fence-then-note-with-braces",
        "prose-then-bare",
        "bare-between-prose",
        "plain-fence",
        "stray-quote-and-brace-in-prose",
    ],
)
def test_coerce_finds_the_object_in_framed_replies(text: str) -> None:
    result = coerce_output(text, _SimpleModel)
    assert result == _SimpleModel(name="framed", value=5)


def test_coerce_ignores_braces_inside_json_strings() -> None:
    text = 'Answer:\n{"name": "uses {curly} and } and \\" quote", "value": 1}\nDone.'
    result = coerce_output(text, _SimpleModel)
    assert result is not None
    assert result.name == 'uses {curly} and } and " quote'


def test_coerce_prefers_the_last_valid_object() -> None:
    text = (
        'First try: {"name": "draft", "value": 1}\n'
        'Corrected: {"name": "final", "value": 2}\n'
        'Unrelated: {"other": true}'
    )
    result = coerce_output(text, _SimpleModel)
    assert result is not None
    assert result.name == "final"


def test_coerce_skips_objects_that_do_not_match_the_schema() -> None:
    text = '{"name": "good", "value": 3}\n\nMetadata: {"tokens": 12}'
    result = coerce_output(text, _SimpleModel)
    assert result is not None
    assert result.name == "good"


def test_coerce_finds_an_object_nested_in_a_wrapper() -> None:
    text = 'Result: {"critique": {"name": "inner", "value": 9}, "note": "{x}"}'
    result = coerce_output(text, _SimpleModel)
    assert result is not None
    assert result.name == "inner"


def test_coerce_prefers_a_top_level_object_over_its_nested_ones() -> None:
    class _Loose(BaseModel):
        name: str = "default"

    text = '{"name": "outer", "child": {"name": "inner"}}'
    result = coerce_output(text, _Loose)
    assert result is not None
    assert result.name == "outer"


def test_coerce_parses_a_nested_schema_in_framed_text() -> None:
    class _Outer(BaseModel):
        inner: _SimpleModel
        tags: list[str]

    text = 'Sure!\n```json\n{"inner": {"name": "n", "value": 4}, "tags": ["a}", "{b"]}\n```'
    result = coerce_output(text, _Outer)
    assert result is not None
    assert result.inner.value == 4
    assert result.tags == ["a}", "{b"]


def test_coerce_returns_none_on_an_unclosed_object() -> None:
    assert coerce_output('Critique:\n{"name": "cut", "value": 1', _SimpleModel) is None


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
