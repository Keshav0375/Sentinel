"""Memory hardening — ``incident_checkpoints`` and ``cache_entries`` (decision 2026-10-25 B/C).

Revision ID: 003
Revises: 002
Create Date: 2026-10-28

- ``incident_checkpoints`` (B): one row per pipeline stage of an in-flight incident, the full
  run snapshot in ``state``, so a restarted pod resumes the incident once instead of losing
  it. The primary key (incident_id, step) is the index on incident_id + step; the rows go
  with their incident (``ON DELETE CASCADE``) and are deleted once the terminal incident is
  stored.
- ``cache_entries`` (C): the tool / LLM result cache behind ``memory.cache.PostgresCache``,
  keyed by a SHA-256 of the canonical request; expired rows are purged at startup.

R14: both hold tool outputs and model replies. They never leave the database — no API
route, LangFuse trace or dataset item reads them. No grant: ``gha-app`` gets nothing new.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "003"
down_revision: str | Sequence[str] | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Agent checkpoints: the latest snapshot of every stage of an in-flight incident
    op.execute(
        """
        CREATE TABLE incident_checkpoints (
            incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
            step INTEGER NOT NULL,
            stage TEXT NOT NULL,
            state JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (incident_id, step)
        )
        """
    )

    # Tool / LLM result cache
    op.execute(
        """
        CREATE TABLE cache_entries (
            key TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            value JSONB NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_cache_entries_expires ON cache_entries(expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE incident_checkpoints")
    op.execute("DROP TABLE cache_entries")
