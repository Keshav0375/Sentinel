"""Initial schema — pgvector extension, incidents, services, deployments (backend.md §5.3).

Revision ID: 001
Revises:
Create Date: 2026-10-11

The DDL is §5.3 verbatim. The ``deployments`` grant to ``"gha-app"`` lets the
deployment pipeline's record stage (ci_app_deployment.yml) insert one row per deploy;
only the table owner can grant, and that is the role running this migration.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Episodic memory: every resolved incident
    op.execute(
        """
        CREATE TABLE incidents (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            alert_id TEXT NOT NULL,
            dd_event_id TEXT,
            correlation_id UUID,
            severity TEXT NOT NULL,
            service TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            root_cause TEXT,
            root_cause_confidence REAL,
            resolution TEXT,
            resolution_type TEXT,  -- 'rollback' | 'hotfix' | 'config_change' | 'escalated'
            pr_number INTEGER,  -- revert PR # (written by GHA right after PR creation)
            pr_url TEXT,  -- revert PR URL (creation record only)
            trajectory JSONB,  -- full agent trace (tool calls, handoffs, reflexion loops)
            eval_score JSONB,  -- judge scores per dimension
            langfuse_trace_id TEXT,
            reflexion_loops INTEGER DEFAULT 0,  -- how many reflexion iterations ran
            created_at TIMESTAMPTZ DEFAULT now(),
            resolved_at TIMESTAMPTZ,
            mttr_seconds INTEGER,  -- mean time to resolution (computed on resolve)
            embedding VECTOR(384)  -- symptom embedding for similarity search
        )
        """
    )

    # Semantic memory: service registry
    op.execute(
        """
        CREATE TABLE services (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name TEXT UNIQUE NOT NULL,
            team TEXT,
            tier TEXT,  -- 'critical' | 'standard' | 'best-effort'
            dependencies JSONB,  -- ["service-a", "service-b"]
            runbook TEXT,  -- markdown runbook content
            metadata JSONB,  -- arbitrary service metadata
            embedding VECTOR(384)  -- service description embedding
        )
        """
    )

    # Deploy tracking: links incidents <-> deploys <-> PRs <-> Datadog.
    # Rows inserted by ci_app_deployment.yml (record-deployment stage) on every deploy.
    op.execute(
        """
        CREATE TABLE deployments (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            service TEXT NOT NULL,
            pr_number INTEGER,
            commit_sha TEXT NOT NULL,
            author TEXT,
            deploy_status TEXT NOT NULL,  -- 'succeeded' | 'failed' | 'rolled_back'
            gha_run_id BIGINT,  -- GHA workflow run ID
            dd_deploy_event_id TEXT,  -- Datadog custom event ID for this deploy
            files_changed JSONB,  -- list of changed files
            incident_id UUID REFERENCES incidents(id),  -- NULL at insert; backfilled on correlation
            deployed_at TIMESTAMPTZ DEFAULT now(),
            metadata JSONB
        )
        """
    )

    op.execute("CREATE INDEX idx_incidents_status ON incidents(status)")
    op.execute("CREATE INDEX idx_incidents_service ON incidents(service)")
    op.execute("CREATE INDEX idx_incidents_correlation ON incidents(correlation_id)")
    op.execute(
        "CREATE INDEX idx_incidents_embedding ON incidents "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10)"
    )
    op.execute(
        "CREATE INDEX idx_services_embedding ON services "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 5)"
    )
    op.execute("CREATE INDEX idx_deployments_service ON deployments(service)")
    op.execute("CREATE INDEX idx_deployments_incident ON deployments(incident_id)")
    op.execute("CREATE INDEX idx_deployments_commit ON deployments(commit_sha)")

    # The pipeline identity records deploys (decision 2026-10-05, R12). Guarded so a
    # local or CI database without the role still migrates; grant-db-access.sh in
    # Sentinel-infra verifies the privilege on the deployed server afterwards.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gha-app') THEN
                GRANT INSERT, SELECT ON deployments TO "gha-app";
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    # deployments first: its incident_id FK references incidents. Dropping a table drops
    # its indexes and grants with it.
    op.execute("DROP TABLE deployments")
    op.execute("DROP TABLE incidents")
    op.execute("DROP TABLE services")
    # The vector extension is deliberately left in place: extensions are per-database
    # objects other schemas may depend on, and on Azure it is allow-listed server-wide
    # (azure.extensions) and managed with the server, not by this app's migrations.
