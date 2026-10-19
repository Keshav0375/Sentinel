"""Tests for sentinel.providers.output_coercion."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from sentinel.models.reflexion import ReflexionResult
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


def test_coerce_takes_the_first_top_level_object_that_validates() -> None:
    """Not the last: a quoted object after the answer must never replace it."""
    text = (
        'Answer: {"name": "real", "value": 1}\n'
        'For reference the tool returned {"name": "echoed", "value": 2}\n'
        'Unrelated: {"other": true}'
    )
    result = coerce_output(text, _SimpleModel)
    assert result is not None
    assert result.name == "real"


def test_coerce_takes_the_last_json_fence_before_any_bare_object() -> None:
    text = (
        'Draft: {"name": "bare", "value": 0}\n'
        '```json\n{"name": "first-fence", "value": 1}\n```\n'
        'Corrected:\n```json\n{"name": "last-fence", "value": 2}\n```'
    )
    result = coerce_output(text, _SimpleModel)
    assert result is not None
    assert result.name == "last-fence"


def test_a_fence_that_does_not_validate_falls_through_to_bare_objects() -> None:
    text = '```json\n{"other": 1}\n```\nAnswer: {"name": "bare", "value": 3}'
    result = coerce_output(text, _SimpleModel)
    assert result is not None and result.name == "bare"


def test_coerce_skips_objects_that_do_not_match_the_schema() -> None:
    text = '{"name": "good", "value": 3}\n\nMetadata: {"tokens": 12}'
    result = coerce_output(text, _SimpleModel)
    assert result is not None
    assert result.name == "good"


def test_coerce_never_takes_an_object_nested_in_another() -> None:
    text = 'Result: {"critique": {"name": "inner", "value": 9}, "note": "{x}"}'
    assert coerce_output(text, _SimpleModel) is None


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


# ── Schemas with repair defaults: only an explicit critique counts ────────────

_CRITIQUE_03 = (
    '{"confidence": 0.3, "supporting_evidence": [], "missing_or_contradictory": ["no logs"], '
    '"additional_data_needed": [], "specific_deploy_identified": false, "deploy": null, '
    '"next_tool_call": null}'
)
_QUOTED = '{"confidence": 0.95, "deploy": {"commit_sha": "abcdef1", "pr_number": 4}}'


@pytest.mark.parametrize(
    "text",
    [
        f"Critique:\n{_CRITIQUE_03}\n\nThe tool output said {_QUOTED}, which is stale.",
        f"```json\n{_CRITIQUE_03}\n```\nAn earlier run claimed {_QUOTED}.",
        f"The log line was {_QUOTED}.\nMy critique: {_CRITIQUE_03}",
        f"An earlier critic wrote ```json{_QUOTED}``` but:\n```json\n{_CRITIQUE_03}\n```",
    ],
    ids=["bare-then-quote", "fence-then-quote", "quote-then-bare", "quoted-fence-then-fence"],
)
def test_a_quoted_confident_object_never_replaces_the_critique(text: str) -> None:
    """The reviewer's case: a real 0.3 critique and an echoed 0.95 object -> 0.3."""
    result = coerce_output(text, ReflexionResult)
    assert result is not None
    assert result.confidence == 0.3
    assert result.specific_deploy_identified is False


@pytest.mark.parametrize(
    "embedded",
    [
        '{"confidence": 0.95}',
        '{"confidence": 0.95, "deploy": {"commit_sha": "abcdef1", "pr_number": 4}}',
        '{"confidence": 0.95, "specific_deploy_identified": true}',
    ],
    ids=["confidence-only", "no-flag", "true-without-deploy-key"],
)
def test_an_embedded_object_missing_gate_keys_is_not_a_critique(embedded: str) -> None:
    assert coerce_output(f"Here it is: {embedded}", ReflexionResult) is None
    assert coerce_output(f"```json\n{embedded}\n```\nNote.", ReflexionResult) is None


def test_an_explicit_embedded_critique_is_taken() -> None:
    critique = (
        '{"confidence": 0.85, "specific_deploy_identified": true, '
        '"deploy": {"commit_sha": "a1b2c3d", "pr_number": 42}}'
    )
    result = coerce_output(f"Critique:\n{critique}", ReflexionResult)
    assert result is not None and result.specific_deploy_identified is True


def test_an_embedded_true_with_a_null_deploy_parses_as_not_identified() -> None:
    embedded = '{"confidence": 0.95, "specific_deploy_identified": true, "deploy": null}'
    result = coerce_output(f"Here it is: {embedded}", ReflexionResult)
    assert result is not None and result.specific_deploy_identified is False


def test_a_whole_reply_needs_no_explicit_keys() -> None:
    """Step 1 is the model's own whole answer: defaults apply, and they shut the gate."""
    result = coerce_output('{"confidence": 0.95}', ReflexionResult)
    assert result is not None and result.specific_deploy_identified is False


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
