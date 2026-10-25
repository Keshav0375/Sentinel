"""Tests for sentinel.models.reverification — the §4.4 verdict token and its parser."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentinel.models.reverification import (
    UNPARSEABLE_REASON,
    ReverificationOutcome,
    ReverificationVerdict,
    parse_reverification,
)

PASS = ReverificationOutcome.PASS
FAIL = ReverificationOutcome.FAIL
ESCALATE = ReverificationOutcome.ESCALATE


@pytest.mark.parametrize("reply", ["PASS", "pass", "  Pass \n", "\n\nPASS\n"])
def test_pass(reply: str) -> None:
    assert parse_reverification(reply) == ReverificationVerdict(outcome=PASS)


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (
            "FAIL: target is pr-41; the first failure follows pr-42",
            "target is pr-41; the first failure follows pr-42",
        ),
        ("fail:wrong service", "wrong service"),
        ("  Fail  :   timing is after the first failure  ", "timing is after the first failure"),
        ("FAIL: x", "x"),
    ],
)
def test_fail_with_reason(reply: str, reason: str) -> None:
    verdict = parse_reverification(reply)
    assert (verdict.outcome, verdict.reason, verdict.parsed) == (FAIL, reason, True)


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("ESCALATE: failures began before any deploy", "failures began before any deploy"),
        ("escalate:y", "y"),
        ("\tEscalate : too uncertain\n", "too uncertain"),
    ],
)
def test_escalate_with_reason(reply: str, reason: str) -> None:
    verdict = parse_reverification(reply)
    assert (verdict.outcome, verdict.reason, verdict.parsed) == (ESCALATE, reason, True)


@pytest.mark.parametrize(
    "reply",
    [
        "",
        None,
        "   ",
        "looks good to me",
        "PASS: all checks hold",  # PASS carries no reason
        "PASS.",
        "**PASS**",
        "`PASS`",
        "FAIL",  # FAIL needs a reason
        "FAIL:",
        "FAIL:   ",
        "ESCALATE",  # ESCALATE needs a reason
        "ESCALATE:",
        "FAIL - wrong target",  # colon required
        "Result: PASS",
        "The fix addresses the root cause.\nPASS",  # exactly one line
        "PASS\nThe fix addresses the root cause.",
        "FAIL: wrong target\nESCALATE: unsure",
        "PASSED",
        "REJECT: no",
    ],
)
def test_anything_else_escalates(reply: str | None) -> None:
    verdict = parse_reverification(reply)
    assert verdict.outcome is ESCALATE
    assert verdict.reason == UNPARSEABLE_REASON
    assert verdict.parsed is False


def test_outcome_values_are_the_tokens() -> None:
    assert [o.value for o in ReverificationOutcome] == ["PASS", "FAIL", "ESCALATE"]


def test_pass_verdict_carries_no_reason() -> None:
    with pytest.raises(ValidationError, match="no reason"):
        ReverificationVerdict(outcome=PASS, reason="fine")


@pytest.mark.parametrize("outcome", [FAIL, ESCALATE])
@pytest.mark.parametrize("reason", [None, "", "  "])
def test_fail_and_escalate_verdicts_require_a_reason(
    outcome: ReverificationOutcome, reason: str | None
) -> None:
    with pytest.raises(ValidationError, match="requires a reason"):
        ReverificationVerdict(outcome=outcome, reason=reason)
