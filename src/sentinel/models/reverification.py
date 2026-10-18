"""Reverification models — the PASS/FAIL/ESCALATE check of a proposed fix (backend §4.4).

The ``reverification`` prompt answers with exactly one line, one of::

    PASS
    FAIL: <reason>
    ESCALATE: <reason>

:func:`parse_reverification` turns that reply into a :class:`ReverificationVerdict`. It is
tolerant of case and of whitespace (around the reply, the keyword and the colon), and
strict about everything else: a reply that is not exactly one of the three forms — extra
lines, text after ``PASS``, a ``FAIL`` or ``ESCALATE`` with no reason, an unknown
keyword — becomes ``ESCALATE``, so an unclear check never proposes a revert PR. Even a
``PASS`` only lets the orchestrator return the rollback spec: GitHub Actions opens the
revert PR and a human merges it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "UNPARSEABLE_REASON",
    "ReverificationOutcome",
    "ReverificationVerdict",
    "parse_reverification",
]

# The reason an unparseable reply escalates with (the raw reply is not echoed: it is
# model output and may be long).
UNPARSEABLE_REASON = "unparseable reverification output"

_PASS = re.compile(r"PASS", re.IGNORECASE)
_WITH_REASON = re.compile(r"(FAIL|ESCALATE)[ \t]*:[ \t]*(\S.*)", re.IGNORECASE)


class ReverificationOutcome(StrEnum):
    """The three §4.4 outcomes."""

    PASS = "PASS"  # create the revert PR (via GitHub Actions; a human merges it)
    FAIL = "FAIL"  # loop back to Resolution with the reason (max 1 retry)
    ESCALATE = "ESCALATE"  # no PR; humans decide


class ReverificationVerdict(BaseModel):
    """A parsed reverification reply.

    ``reason`` is None for PASS and a non-empty string for FAIL and ESCALATE. ``parsed`` is
    False when the reply did not follow the format (the verdict is then ESCALATE with
    :data:`UNPARSEABLE_REASON`).
    """

    outcome: ReverificationOutcome
    reason: str | None = Field(default=None, description="Why FAIL or ESCALATE; None for PASS.")
    parsed: bool = Field(default=True, description="False when the reply was malformed.")

    @model_validator(mode="after")
    def _reason_matches_outcome(self) -> Self:
        if self.outcome is ReverificationOutcome.PASS:
            if self.reason is not None:
                raise ValueError("PASS carries no reason")
        elif not (self.reason and self.reason.strip()):
            raise ValueError(f"{self.outcome.value} requires a reason")
        return self


def parse_reverification(text: str | None) -> ReverificationVerdict:
    """Parse a reverification reply; anything but the documented format → ESCALATE."""
    reply = (text or "").strip()
    if "\n" in reply or "\r" in reply:
        return _unparseable()
    if _PASS.fullmatch(reply):
        return ReverificationVerdict(outcome=ReverificationOutcome.PASS)
    match = _WITH_REASON.fullmatch(reply)
    if match:
        outcome = ReverificationOutcome(match.group(1).upper())
        return ReverificationVerdict(outcome=outcome, reason=match.group(2).strip())
    return _unparseable()


def _unparseable() -> ReverificationVerdict:
    return ReverificationVerdict(
        outcome=ReverificationOutcome.ESCALATE, reason=UNPARSEABLE_REASON, parsed=False
    )
