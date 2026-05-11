"""Service models — ServiceTier, ServiceMetadata."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ServiceTier(StrEnum):
    """Operational tier determines alert escalation priority and SLA.

    Values match the SQLite services.tier column (ARCHITECTURE.md §7.3).
    """

    CRITICAL = "critical"
    STANDARD = "standard"
    BEST_EFFORT = "best-effort"


class ServiceMetadata(BaseModel):
    """Ownership and topology data for a single microservice.

    Loaded from the semantic memory store (SQLite services table) and passed
    to the Triage and Log Analyst agents so they can route alerts and
    understand blast radius.
    """

    name: str
    team: str
    tier: ServiceTier
    oncall_channel: str | None = None
    repo_url: str | None = None
    description: str = ""
    dependencies: list[str] = Field(default_factory=list)
