"""Deploy models — Deploy, DeployCorrelation."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class Deploy(BaseModel):
    """A single deployment event for a service.

    Matches the deploy objects in scenario JSON files:
    {"id": "...", "service": "...", "ts": "...", "author": "...", ...}
    """

    id: str
    service: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    author: str
    commit_sha: str
    files_changed: int
    description: str = ""


class DeployCorrelation(BaseModel):
    """Output of the Deploy Correlator agent.

    suspect_deploy is None when no deploy is convincingly correlated to the
    incident (e.g. a downstream outage scenario with no recent deploys).
    confidence is in [0.0, 1.0].
    """

    recent_deploys: list[Deploy] = []
    suspect_deploy: Deploy | None = None
    confidence: float = 0.0
    evidence: str
