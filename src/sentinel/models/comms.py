"""Communications models — SlackSummary."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SlackSummary(BaseModel):
    """Structured output produced by the Comms Agent.

    ``slack_message`` is the full mrkdwn-formatted message ready to post to
    Slack. ``recipients`` lists the channels it should be sent to — always
    ``#incidents``; P1/P2 incidents also include the service team's oncall
    channel (e.g. ``#oncall-platform``).
    """

    slack_message: str
    recipients: list[str] = Field(default_factory=lambda: ["#incidents"])
