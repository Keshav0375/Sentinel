"""Seed the service registry — one row, ``sentinel-watchtower`` (backend.md §5.3, §13.3).

Revision ID: 002
Revises: 001
Create Date: 2026-10-11

Replaces the Phase-1 ``data/services/*.json`` + ``seed.py``. Sentinel watches exactly one
real service — the app ``Sentinel-deployment`` deploys to App Service — so that is the only
row (decision 2026-10-10). The acme-corp Phase-1 registry is not seeded.

The embedding is computed here, at migration time, with the app's own
:class:`~sentinel.memory.embeddings.EmbeddingClient` and ``SENTINEL_EMBEDDING_MODEL``
(all-MiniLM-L6-v2, 384 dims), so symptom search works as soon as the migration lands.
The first run on a machine downloads the model from the Hugging Face Hub (~90 MB, then
cached). Callers driving alembic programmatically may pass a ready client in
``config.attributes["embedding_client"]`` (tests do, to load the model once per session).

``idx_services_embedding`` was built by 001 on an empty table, so its IVFFlat lists carry
no trained centroids; it is rebuilt after the insert.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

import sqlalchemy as sa
from alembic import context, op

from sentinel.config import Settings
from sentinel.memory.embeddings import EmbeddingClient

revision: str = "002"
down_revision: str | Sequence[str] | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SERVICE_NAME = "sentinel-watchtower"
TEAM = "sentinel"
TIER = "standard"
DEPENDENCIES: list[str] = []
METADATA = {"repo": "Keshav0375/Sentinel-deployment", "platform": "azure-app-service-f1"}

RUNBOOK = """\
# sentinel-watchtower — rollback runbook

The app deployed from Keshav0375/Sentinel-deployment to Azure App Service. Every merge to
`main` deploys as version `pr-<N>-<sha>` and is recorded in the `deployments` table.

## When an alert fires for sentinel-watchtower

1. Identify the culprit merge: the `deployments` row for `sentinel-watchtower` deployed
   just before the alert, version `pr-<N>-<sha>` (PR number and commit sha).
2. Open a revert PR on Keshav0375/Sentinel-deployment that reverts that merge.
3. A human reviews and merges the revert PR. The merge redeploys the previous code;
   wait for the deploy run to go green and the health check to recover.

Never deploy, merge or roll back directly: the revert PR is the approval gate.

See `Sentinel-deployment/scenarios/README.md` for the scenario branches and how each one
is reverted.
"""


def embedding_text() -> str:
    """The text the service embedding is computed from: name, team, tier and runbook."""
    return f"{SERVICE_NAME}\nteam: {TEAM}\ntier: {TIER}\n\n{RUNBOOK}"


def _embedding_model_name() -> str:
    """``Settings.sentinel_embedding_model`` (env / ``.env`` / default).

    Migrations need no LLM provider, so a missing GROQ_API_KEY must not stop them; the
    placeholder only satisfies Settings' required field and is never used.
    """
    groq_key = os.environ.get("GROQ_API_KEY", "").strip() or "unused-by-migrations"
    return Settings(groq_api_key=groq_key).sentinel_embedding_model


def _embed(text: str) -> list[float]:
    """Embed *text* with the injected client, else the configured model.

    Online migrations run inside env.py's event loop (``run_sync``), so the async client
    is driven on a worker thread with its own loop.
    """
    client = context.config.attributes.get("embedding_client")
    if client is None:
        client = EmbeddingClient.from_model_name(_embedding_model_name())
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, client.embed(text)).result()


def upgrade() -> None:
    vector = _embed(embedding_text())
    op.execute(
        sa.text(
            """
            INSERT INTO services (name, team, tier, dependencies, runbook, metadata, embedding)
            VALUES (:name, :team, :tier, CAST(:dependencies AS jsonb), :runbook,
                    CAST(:metadata AS jsonb), CAST(:embedding AS vector))
            ON CONFLICT (name) DO UPDATE SET
                team = EXCLUDED.team,
                tier = EXCLUDED.tier,
                dependencies = EXCLUDED.dependencies,
                runbook = EXCLUDED.runbook,
                metadata = EXCLUDED.metadata,
                embedding = EXCLUDED.embedding
            """
        ).bindparams(
            name=SERVICE_NAME,
            team=TEAM,
            tier=TIER,
            dependencies=json.dumps(DEPENDENCIES),
            runbook=RUNBOOK,
            metadata=json.dumps(METADATA),
            embedding="[" + ",".join(repr(float(x)) for x in vector) + "]",
        )
    )
    # Train the IVFFlat lists on the seeded data (001 built the index on an empty table).
    op.execute("REINDEX INDEX idx_services_embedding")


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM services WHERE name = :name").bindparams(name=SERVICE_NAME))
