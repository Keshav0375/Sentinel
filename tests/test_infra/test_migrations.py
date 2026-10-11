"""Tests for the alembic migrations — ``001_initial_schema`` and ``002_seed_services``
(backend.md §5.3).

Tests taking ``fresh_db`` create a throwaway database on the pgvector container named by
DATABASE_URL (see tests/conftest.py), so ``upgrade head`` is proven from empty, and drop
it afterwards. They skip with a reason when DATABASE_URL is not set.

Migrations run through ``alembic.command`` (sync; env.py owns its own event loop), so
these tests are sync and query the database with ``asyncio.run``. ``002`` embeds its row
with the real sentence-transformers model, loaded once per session
(``seed_embedding_client``); only the offline ``--sql`` test, which needs no database,
injects a fake.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import numpy as np
import pytest
from alembic import command
from alembic.config import Config
from alembic.util import CommandError

from sentinel.memory.embeddings import EmbeddingClient

# ── Expected schema (§5.3, verbatim) ──────────────────────────────────────────
# column → (format_type, NOT NULL, default expression or None), in declaration order.

_EXPECTED_COLUMNS: dict[str, list[tuple[str, str, bool, str | None]]] = {
    "incidents": [
        ("id", "uuid", True, "gen_random_uuid()"),
        ("alert_id", "text", True, None),
        ("dd_event_id", "text", False, None),
        ("correlation_id", "uuid", False, None),
        ("severity", "text", True, None),
        ("service", "text", True, None),
        ("status", "text", True, "'open'::text"),
        ("root_cause", "text", False, None),
        ("root_cause_confidence", "real", False, None),
        ("resolution", "text", False, None),
        ("resolution_type", "text", False, None),
        ("pr_number", "integer", False, None),
        ("pr_url", "text", False, None),
        ("trajectory", "jsonb", False, None),
        ("eval_score", "jsonb", False, None),
        ("langfuse_trace_id", "text", False, None),
        ("reflexion_loops", "integer", False, "0"),
        ("created_at", "timestamp with time zone", False, "now()"),
        ("resolved_at", "timestamp with time zone", False, None),
        ("mttr_seconds", "integer", False, None),
        ("embedding", "vector(384)", False, None),
    ],
    "services": [
        ("id", "uuid", True, "gen_random_uuid()"),
        ("name", "text", True, None),
        ("team", "text", False, None),
        ("tier", "text", False, None),
        ("dependencies", "jsonb", False, None),
        ("runbook", "text", False, None),
        ("metadata", "jsonb", False, None),
        ("embedding", "vector(384)", False, None),
    ],
    "deployments": [
        ("id", "uuid", True, "gen_random_uuid()"),
        ("service", "text", True, None),
        ("pr_number", "integer", False, None),
        ("commit_sha", "text", True, None),
        ("author", "text", False, None),
        ("deploy_status", "text", True, None),
        ("gha_run_id", "bigint", False, None),
        ("dd_deploy_event_id", "text", False, None),
        ("files_changed", "jsonb", False, None),
        ("incident_id", "uuid", False, None),
        ("deployed_at", "timestamp with time zone", False, "now()"),
        ("metadata", "jsonb", False, None),
    ],
}

# information_schema.columns.data_type for each format_type above.
_INFO_SCHEMA_TYPE = {
    "uuid": "uuid",
    "text": "text",
    "real": "real",
    "integer": "integer",
    "bigint": "bigint",
    "jsonb": "jsonb",
    "timestamp with time zone": "timestamp with time zone",
    "vector(384)": "USER-DEFINED",
}

# The 8 §5.3 indexes, as pg_indexes.indexdef renders them.
_EXPECTED_INDEXES = {
    "idx_incidents_status": "CREATE INDEX idx_incidents_status ON public.incidents "
    "USING btree (status)",
    "idx_incidents_service": "CREATE INDEX idx_incidents_service ON public.incidents "
    "USING btree (service)",
    "idx_incidents_correlation": "CREATE INDEX idx_incidents_correlation ON public.incidents "
    "USING btree (correlation_id)",
    "idx_incidents_embedding": "CREATE INDEX idx_incidents_embedding ON public.incidents "
    "USING ivfflat (embedding vector_cosine_ops) WITH (lists='10')",
    "idx_services_embedding": "CREATE INDEX idx_services_embedding ON public.services "
    "USING ivfflat (embedding vector_cosine_ops) WITH (lists='5')",
    "idx_deployments_service": "CREATE INDEX idx_deployments_service ON public.deployments "
    "USING btree (service)",
    "idx_deployments_incident": "CREATE INDEX idx_deployments_incident ON public.deployments "
    "USING btree (incident_id)",
    "idx_deployments_commit": "CREATE INDEX idx_deployments_commit ON public.deployments "
    "USING btree (commit_sha)",
}

# Constraint-backed indexes: the three primary keys and services.name UNIQUE.
_CONSTRAINT_INDEXES = {"incidents_pkey", "services_pkey", "deployments_pkey", "services_name_key"}

_TABLES = {"incidents", "services", "deployments"}

# The record stage's INSERT, verbatim from Sentinel-deployment ci_app_deployment.yml.
# psql substitutes each :'var' as a quoted literal; _psql_render does the same.
_WRITER_INSERT = """
INSERT INTO deployments
  (service, pr_number, commit_sha, author, deploy_status, gha_run_id, files_changed, metadata)
VALUES
  (:'service', :'pr'::int, :'sha', :'author', :'status', :'run'::bigint, :'files'::jsonb,
   jsonb_build_object('failed_stage', :'stage', 'version', :'version'));
"""

_GHA_ROLE = "gha-app"

_HEAD = "002"


# ── Helpers / fixtures ────────────────────────────────────────────────────────


def _with_database(dsn: str, name: str) -> str:
    """*dsn* pointed at database *name*."""
    return urlunsplit(urlsplit(dsn)._replace(path=f"/{name}"))


def _run(coro_fn: Callable[[asyncpg.Connection], Any], dsn: str) -> Any:
    """Open a connection to *dsn*, await ``coro_fn(conn)``, close it."""

    async def _go() -> Any:
        conn = await asyncpg.connect(dsn)
        try:
            return await coro_fn(conn)
        finally:
            await conn.close()

    return asyncio.run(_go())


def _execute(dsn: str, sql: str) -> None:
    async def _do(conn: asyncpg.Connection) -> None:
        await conn.execute(sql)

    _run(_do, dsn)


def _psql_render(template: str, values: dict[str, str]) -> str:
    """Substitute psql ``:'name'`` variables as quoted literals, as ``psql -v`` does."""

    def _quote(match: re.Match[str]) -> str:
        return "'" + values[match.group(1)].replace("'", "''") + "'"

    return re.sub(r":'(\w+)'", _quote, template)


@pytest.fixture
def fresh_db(pg_dsn: str) -> Iterator[str]:
    """An empty throwaway database on the test server; dropped afterwards."""
    name = f"sentinel_migr_{uuid.uuid4().hex[:12]}"
    _execute(pg_dsn, f'CREATE DATABASE "{name}"')
    try:
        yield _with_database(pg_dsn, name)
    finally:
        _execute(pg_dsn, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def cfg(
    fresh_db: str,
    alembic_config: Callable[..., Config],
    seed_embedding_client: EmbeddingClient,
) -> Config:
    return alembic_config(fresh_db, seed_embedding_client)


def _drop_gha_role(dsn: str) -> None:
    """Drop the server-wide pipeline role, revoking what it holds in *dsn*'s database first.

    ``DROP OWNED`` clears a grant left on the main test database by an aborted run;
    grants in throwaway databases go with those databases.
    """
    _execute(
        dsn,
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_GHA_ROLE}') THEN
                DROP OWNED BY "{_GHA_ROLE}";
                DROP ROLE "{_GHA_ROLE}";
            END IF;
        END
        $$
        """,
    )


@pytest.fixture
def gha_role(pg_dsn: str) -> Iterator[str]:
    """The pipeline role, created NOLOGIN for the test and dropped afterwards.

    Request it *before* ``fresh_db``/``cfg`` so it is torn down after the throwaway
    database (and the grant inside it) is gone.
    """
    _drop_gha_role(pg_dsn)
    _execute(pg_dsn, f'CREATE ROLE "{_GHA_ROLE}" NOLOGIN')
    try:
        yield _GHA_ROLE
    finally:
        _drop_gha_role(pg_dsn)


@pytest.fixture
def no_gha_role(pg_dsn: str) -> str:
    """Guarantee the pipeline role does not exist on the server."""
    _drop_gha_role(pg_dsn)
    return _GHA_ROLE


async def _schema_snapshot(conn: asyncpg.Connection) -> dict[str, Any]:
    """Everything §5.3 defines, read back from the catalogs."""
    tables = await conn.fetch(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog', "
        "'information_schema')"
    )
    columns = await conn.fetch(
        """
        SELECT c.table_name, c.column_name, c.data_type, c.udt_name, c.is_nullable,
               c.column_default, c.ordinal_position,
               format_type(a.atttypid, a.atttypmod) AS full_type
        FROM information_schema.columns c
        JOIN pg_attribute a
          ON a.attrelid = format('%I.%I', c.table_schema, c.table_name)::regclass
         AND a.attname = c.column_name
        WHERE c.table_schema = 'public' AND c.table_name = ANY($1::text[])
        ORDER BY c.table_name, c.ordinal_position
        """,
        sorted(_TABLES),
    )
    indexes = await conn.fetch(
        "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
    )
    extension = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    constraints = await conn.fetch(
        """
        SELECT conrelid::regclass::text AS tbl, contype::text AS contype,
               pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE connamespace = 'public'::regnamespace
        """
    )
    return {
        "tables": {(r["table_schema"], r["table_name"]) for r in tables},
        "columns": [dict(r) for r in columns],
        "indexes": {r["indexname"]: r["indexdef"] for r in indexes},
        "extension": extension,
        "constraints": {(r["tbl"], r["contype"], r["def"]) for r in constraints},
    }


def _snapshot(dsn: str) -> dict[str, Any]:
    return _run(_schema_snapshot, dsn)


def _assert_full_schema(dsn: str) -> None:
    snap = _snapshot(dsn)

    assert snap["extension"] is not None, "vector extension missing"
    assert snap["tables"] == {("public", t) for t in _TABLES} | {("public", "alembic_version")}

    for table, expected in _EXPECTED_COLUMNS.items():
        actual = [c for c in snap["columns"] if c["table_name"] == table]
        assert [c["column_name"] for c in actual] == [e[0] for e in expected], table
        for col, (name, full_type, not_null, default) in zip(actual, expected, strict=True):
            where = f"{table}.{name}"
            assert col["full_type"] == full_type, where
            assert col["data_type"] == _INFO_SCHEMA_TYPE[full_type], where
            assert (col["is_nullable"] == "NO") is not_null, where
            assert col["column_default"] == default, where
        vector_cols = [c for c in actual if c["full_type"] == "vector(384)"]
        assert all(c["udt_name"] == "vector" for c in vector_cols), table

    # Exactly the 8 §5.3 indexes plus the constraint-backed ones — nothing extra.
    assert set(snap["indexes"]) == set(_EXPECTED_INDEXES) | _CONSTRAINT_INDEXES | {
        "alembic_version_pkc"
    }
    for name, indexdef in _EXPECTED_INDEXES.items():
        assert snap["indexes"][name] == indexdef

    # No CHECKs, enums or other constraints beyond §5.3 (alembic_version's pkey aside).
    assert {c for c in snap["constraints"] if c[0] != "alembic_version"} == {
        ("incidents", "p", "PRIMARY KEY (id)"),
        ("services", "p", "PRIMARY KEY (id)"),
        ("services", "u", "UNIQUE (name)"),
        ("deployments", "p", "PRIMARY KEY (id)"),
        ("deployments", "f", "FOREIGN KEY (incident_id) REFERENCES incidents(id)"),
    }


def _assert_empty_schema(dsn: str) -> None:
    snap = _snapshot(dsn)
    assert snap["tables"] <= {("public", "alembic_version")}
    assert not snap["columns"]
    assert set(snap["indexes"]) <= {"alembic_version_pkc"}


def _current_revision(dsn: str) -> str | None:
    async def _rev(conn: asyncpg.Connection) -> str | None:
        exists = await conn.fetchval("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        if not exists:
            return None
        return await conn.fetchval("SELECT version_num FROM alembic_version")

    return _run(_rev, dsn)


def _writer_insert(dsn: str, status: str, *, role: str | None = None) -> asyncpg.Record:
    """Run the pipeline's INSERT (optionally as *role*) and return the stored row."""
    sql = _psql_render(
        _WRITER_INSERT,
        {
            "service": "sentinel-watchtower",
            "pr": "42",
            "sha": "abc1234",
            "author": "keshav",
            "status": status,
            "run": "12345678901",
            "files": json.dumps(["app/main.py", "app/db.py"]),
            "stage": "none" if status == "succeeded" else "smoke",
            "version": "v1.2.3",
        },
    )

    async def _do(conn: asyncpg.Connection) -> asyncpg.Record:
        if role is not None:
            await conn.execute(f'SET ROLE "{role}"')
        # Simple-query protocol, no parameters — exactly what psql sends.
        await conn.execute(sql)
        row = await conn.fetchrow(
            "SELECT * FROM deployments WHERE deploy_status = $1 ORDER BY deployed_at DESC LIMIT 1",
            status,
        )
        assert row is not None
        return row

    return _run(_do, dsn)


# ── Upgrade / downgrade ───────────────────────────────────────────────────────


def test_upgrade_from_empty_creates_full_schema(fresh_db: str, cfg: Config) -> None:
    assert _current_revision(fresh_db) is None
    assert _snapshot(fresh_db)["tables"] == set()

    command.upgrade(cfg, "head")

    assert _current_revision(fresh_db) == _HEAD
    _assert_full_schema(fresh_db)


def test_upgrade_creates_vector_extension(fresh_db: str, cfg: Config) -> None:
    # Whatever the server's template database carries, 001 must create the extension.
    _execute(fresh_db, "DROP EXTENSION IF EXISTS vector")
    assert _snapshot(fresh_db)["extension"] is None

    command.upgrade(cfg, "head")

    assert _snapshot(fresh_db)["extension"] is not None


def test_upgrade_head_is_idempotent(fresh_db: str, cfg: Config) -> None:
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    assert _current_revision(fresh_db) == _HEAD
    _assert_full_schema(fresh_db)


def test_downgrade_removes_schema_keeps_extension(fresh_db: str, cfg: Config) -> None:
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    _assert_empty_schema(fresh_db)
    assert _current_revision(fresh_db) is None
    # Shared, allow-listed on Azure — never dropped by app migrations.
    assert _snapshot(fresh_db)["extension"] is not None


def test_downgrade_with_linked_rows(fresh_db: str, cfg: Config) -> None:
    """The FK from deployments to incidents does not block downgrade."""
    command.upgrade(cfg, "head")
    _execute(
        fresh_db,
        """
        WITH i AS (
            INSERT INTO incidents (alert_id, severity, service)
            VALUES ('a-1', 'P1', 'sentinel-watchtower') RETURNING id
        )
        INSERT INTO deployments (service, commit_sha, deploy_status, incident_id)
        SELECT 'sentinel-watchtower', 'abc1234', 'failed', id FROM i
        """,
    )

    command.downgrade(cfg, "base")

    _assert_empty_schema(fresh_db)


def test_round_trip_upgrade_downgrade_upgrade(fresh_db: str, cfg: Config) -> None:
    command.upgrade(cfg, "head")
    first = _snapshot(fresh_db)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    _assert_full_schema(fresh_db)
    second = _snapshot(fresh_db)
    assert second["columns"] == first["columns"]
    assert second["indexes"] == first["indexes"]
    assert second["constraints"] == first["constraints"]


# ── Defaults and the writer contract ──────────────────────────────────────────


def test_column_defaults_apply(fresh_db: str, cfg: Config) -> None:
    command.upgrade(cfg, "head")

    async def _do(conn: asyncpg.Connection) -> asyncpg.Record:
        return await conn.fetchrow(
            "INSERT INTO incidents (alert_id, severity, service) "
            "VALUES ('a-1', 'P2', 'sentinel-watchtower') "
            "RETURNING id, status, reflexion_loops, created_at"
        )

    row = _run(_do, fresh_db)
    assert isinstance(row["id"], uuid.UUID)
    assert row["status"] == "open"
    assert row["reflexion_loops"] == 0
    assert row["created_at"] is not None


def test_services_name_is_unique(fresh_db: str, cfg: Config) -> None:
    command.upgrade(cfg, "head")
    _execute(fresh_db, "INSERT INTO services (name) VALUES ('uniqueness-probe')")
    with pytest.raises(asyncpg.UniqueViolationError):
        _execute(fresh_db, "INSERT INTO services (name) VALUES ('uniqueness-probe')")


def test_deployments_incident_fk_enforced(fresh_db: str, cfg: Config) -> None:
    command.upgrade(cfg, "head")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        _execute(
            fresh_db,
            "INSERT INTO deployments (service, commit_sha, deploy_status, incident_id) "
            f"VALUES ('s', 'abc', 'failed', '{uuid.uuid4()}')",
        )


@pytest.mark.parametrize("status", ["succeeded", "failed", "rolled_back"])
def test_writer_insert_shape_succeeds(fresh_db: str, cfg: Config, status: str) -> None:
    command.upgrade(cfg, "head")

    row = _writer_insert(fresh_db, status)

    assert row["service"] == "sentinel-watchtower"
    assert row["pr_number"] == 42
    assert row["commit_sha"] == "abc1234"
    assert row["author"] == "keshav"
    assert row["deploy_status"] == status
    assert row["gha_run_id"] == 12345678901  # > int4 — needs BIGINT
    assert json.loads(row["files_changed"]) == ["app/main.py", "app/db.py"]
    assert json.loads(row["metadata"]) == {
        "failed_stage": "none" if status == "succeeded" else "smoke",
        "version": "v1.2.3",
    }
    assert row["incident_id"] is None
    assert isinstance(row["id"], uuid.UUID)
    assert row["deployed_at"] is not None


# ── The gha-app grant (R12) ───────────────────────────────────────────────────


def _privileges(dsn: str, role: str) -> tuple[bool, bool]:
    async def _do(conn: asyncpg.Connection) -> tuple[bool, bool]:
        row = await conn.fetchrow(
            "SELECT has_table_privilege($1::name, 'public.deployments', 'INSERT') AS ins, "
            "has_table_privilege($1::name, 'public.deployments', 'SELECT') AS sel",
            role,
        )
        assert row is not None
        return bool(row["ins"]), bool(row["sel"])

    return _run(_do, dsn)


def test_grant_to_gha_app_when_role_exists(gha_role: str, fresh_db: str, cfg: Config) -> None:
    command.upgrade(cfg, "head")

    # The exact check Sentinel-infra/scripts/grant-db-access.sh runs.
    assert _privileges(fresh_db, gha_role) == (True, True)

    # And the grant is sufficient for the pipeline's real INSERT, run as the role.
    row = _writer_insert(fresh_db, "succeeded", role=gha_role)
    assert row["deploy_status"] == "succeeded"


def test_gha_app_has_nothing_beyond_the_grant(gha_role: str, fresh_db: str, cfg: Config) -> None:
    command.upgrade(cfg, "head")

    async def _do(conn: asyncpg.Connection) -> list[tuple[str, str]]:
        rows = await conn.fetch(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = $1 ORDER BY 1, 2",
            gha_role,
        )
        return [(r["table_name"], r["privilege_type"]) for r in rows]

    assert _run(_do, fresh_db) == [("deployments", "INSERT"), ("deployments", "SELECT")]


def test_upgrade_without_gha_app_role_succeeds(
    no_gha_role: str, fresh_db: str, cfg: Config
) -> None:
    command.upgrade(cfg, "head")

    _assert_full_schema(fresh_db)
    _writer_insert(fresh_db, "failed")


# ── 002_seed_services ─────────────────────────────────────────────────────────

_SEED_MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "002_seed_services.py"
)

_SEED_NAME = "sentinel-watchtower"

_SYMPTOM = "sentinel-watchtower health check failing after deploy, rollback"

# Test-only decoys (never seeded): unrelated services the symptom must not rank above.
_DECOYS = {
    "payments-api": "payments-api\nteam: billing\ntier: critical\n\nCard authorization and "
    "refunds. On a rising decline rate, inspect the card-network webhook queue.",
    "search-indexer": "search-indexer\nteam: discovery\ntier: best-effort\n\nRebuilds the "
    "product search index nightly. On indexing lag, restart the queue consumer.",
}


class _ConstantModel:
    """A stand-in sentence-transformers model: every text embeds to ``[value] * 384``."""

    def __init__(self, value: float) -> None:
        self._value = value

    def encode(self, texts: list[str], **_: Any) -> list[np.ndarray]:
        return [np.full(384, self._value, dtype=np.float32) for _ in texts]


def _seed_module() -> ModuleType:
    """The 002 migration module, for its seed constants (its file name is not importable)."""
    spec = importlib.util.spec_from_file_location("seed_services_002", _SEED_MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _embed(client: EmbeddingClient, text: str) -> list[float]:
    return asyncio.run(client.embed(text))


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _services(dsn: str) -> list[dict[str, Any]]:
    """Every services row, ordered by name; JSONB and vector columns decoded."""

    async def _do(conn: asyncpg.Connection) -> list[asyncpg.Record]:
        return await conn.fetch(
            "SELECT id, name, team, tier, dependencies, runbook, metadata, "
            "embedding::text AS embedding, vector_dims(embedding) AS dims "
            "FROM services ORDER BY name"
        )

    rows = []
    for r in _run(_do, dsn):
        row = dict(r)
        for col in ("dependencies", "metadata", "embedding"):
            row[col] = None if row[col] is None else json.loads(row[col])
        rows.append(row)
    return rows


def _insert_service(dsn: str, name: str, embedding: list[float] | None = None) -> None:
    async def _do(conn: asyncpg.Connection) -> None:
        await conn.execute(
            "INSERT INTO services (name, team, tier, dependencies, runbook, metadata, embedding) "
            "VALUES ($1, 'other', 'critical', '[\"db\"]', 'old runbook', '{}', "
            "CAST($2::text AS vector))",
            name,
            None if embedding is None else _vector_literal(embedding),
        )

    _run(_do, dsn)


def _ivfflat_ranking(dsn: str, query: list[float], *, probes: int | None = None) -> list[str]:
    """Service names nearest *query* by cosine, read through ``idx_services_embedding``.

    Sequential scans are disabled so the planner must use the IVFFlat index even on a
    tiny table; the plan is asserted, not assumed. *probes* None keeps pgvector's default.
    """

    async def _do(conn: asyncpg.Connection) -> list[str]:
        sql = "SELECT name FROM services ORDER BY embedding <=> CAST($1::text AS vector) LIMIT 10"
        literal = _vector_literal(query)
        async with conn.transaction():
            await conn.execute("SET LOCAL enable_seqscan = off")
            if probes is not None:
                await conn.execute(f"SET LOCAL ivfflat.probes = {probes}")
            plan = "\n".join(r[0] for r in await conn.fetch(f"EXPLAIN {sql}", literal))
            assert "Index Scan using idx_services_embedding on services" in plan, plan
            return [r["name"] for r in await conn.fetch(sql, literal)]

    return _run(_do, dsn)


def _assert_seed_row(
    row: dict[str, Any], seed: ModuleType, expected_embedding: list[float]
) -> None:
    assert row["name"] == _SEED_NAME
    assert row["team"] == "sentinel"
    assert row["tier"] == "standard"
    assert row["dependencies"] == []
    assert row["metadata"] == {
        "repo": "Keshav0375/Sentinel-deployment",
        "platform": "azure-app-service-f1",
    }
    assert row["runbook"] == seed.RUNBOOK
    assert row["dims"] == 384
    assert row["embedding"] == pytest.approx(expected_embedding, abs=1e-6)


def test_seed_inserts_exactly_sentinel_watchtower(
    fresh_db: str, cfg: Config, seed_embedding_client: EmbeddingClient
) -> None:
    command.upgrade(cfg, "head")

    rows = _services(fresh_db)
    assert [r["name"] for r in rows] == [_SEED_NAME]
    seed = _seed_module()
    _assert_seed_row(rows[0], seed, _embed(seed_embedding_client, seed.embedding_text()))


def test_seed_runbook_is_the_revert_pr_rollback(fresh_db: str, cfg: Config) -> None:
    command.upgrade(cfg, "head")

    runbook = _services(fresh_db)[0]["runbook"]
    assert runbook.startswith("# sentinel-watchtower")
    for phrase in (
        "`deployments` table",
        "`pr-<N>-<sha>`",
        "Open a revert PR on Keshav0375/Sentinel-deployment",
        "A human reviews and merges the revert PR",
        "redeploys the previous code",
        "`Sentinel-deployment/scenarios/README.md`",
    ):
        assert phrase in runbook, phrase
    # The steps run in rollback order: culprit → revert PR → human merge.
    assert (
        runbook.index("culprit merge")
        < runbook.index("Open a revert PR")
        < runbook.index("A human reviews")
    )


def test_seed_embedding_covers_name_team_tier_runbook(
    fresh_db: str, cfg: Config, seed_embedding_client: EmbeddingClient
) -> None:
    command.upgrade(cfg, "head")
    seed = _seed_module()

    text = seed.embedding_text()
    for part in (_SEED_NAME, "team: sentinel", "tier: standard", seed.RUNBOOK):
        assert part in text
    row = _services(fresh_db)[0]
    assert row["embedding"] is not None
    assert row["dims"] == 384
    # Not the embedding of the runbook (or name) alone.
    assert row["embedding"] != pytest.approx(_embed(seed_embedding_client, seed.RUNBOOK), abs=1e-6)
    assert row["embedding"] != pytest.approx(_embed(seed_embedding_client, _SEED_NAME), abs=1e-6)


def test_symptom_query_ranks_watchtower_first_via_ivfflat(
    fresh_db: str, cfg: Config, seed_embedding_client: EmbeddingClient
) -> None:
    command.upgrade(cfg, "head")
    symptom = _embed(seed_embedding_client, _SYMPTOM)

    # Seeded table, default probes: the index rebuilt by 002 finds the row.
    assert _ivfflat_ranking(fresh_db, symptom) == [_SEED_NAME]

    for name, text in _DECOYS.items():
        _insert_service(fresh_db, name, _embed(seed_embedding_client, text))
    # Every list probed, so the ranking is the exact cosine order — still via the index.
    ranking = _ivfflat_ranking(fresh_db, symptom, probes=5)
    assert ranking[0] == _SEED_NAME
    assert sorted(ranking) == sorted([_SEED_NAME, *_DECOYS])


def test_seed_upsert_overwrites_existing_row(
    fresh_db: str, cfg: Config, seed_embedding_client: EmbeddingClient
) -> None:
    """ON CONFLICT (name) DO UPDATE: a pre-existing row is replaced, never duplicated."""
    command.upgrade(cfg, "001")
    _insert_service(fresh_db, _SEED_NAME)  # stale values, no embedding

    command.upgrade(cfg, "head")

    rows = _services(fresh_db)
    assert len(rows) == 1
    seed = _seed_module()
    _assert_seed_row(rows[0], seed, _embed(seed_embedding_client, seed.embedding_text()))


def test_seed_rerun_is_idempotent(fresh_db: str, cfg: Config) -> None:
    command.upgrade(cfg, "head")
    before = _services(fresh_db)

    # Run 002 again over its own row (stamp back, then upgrade).
    command.stamp(cfg, "001")
    command.upgrade(cfg, "head")

    assert _current_revision(fresh_db) == _HEAD
    after = _services(fresh_db)
    assert after == before  # same single row, same id, same values
    _assert_full_schema(fresh_db)


def test_downgrade_to_001_removes_only_the_seed(fresh_db: str, cfg: Config) -> None:
    command.upgrade(cfg, "head")
    _insert_service(fresh_db, "other-service")
    _execute(
        fresh_db,
        """
        WITH i AS (
            INSERT INTO incidents (alert_id, severity, service)
            VALUES ('a-1', 'P1', 'sentinel-watchtower') RETURNING id
        )
        INSERT INTO deployments (service, commit_sha, deploy_status, incident_id)
        SELECT 'sentinel-watchtower', 'abc1234', 'failed', id FROM i
        """,
    )

    command.downgrade(cfg, "001")

    assert _current_revision(fresh_db) == "001"
    assert [r["name"] for r in _services(fresh_db)] == ["other-service"]

    async def _counts(conn: asyncpg.Connection) -> tuple[int, int]:
        return (
            await conn.fetchval("SELECT count(*) FROM incidents"),
            await conn.fetchval("SELECT count(*) FROM deployments"),
        )

    assert _run(_counts, fresh_db) == (1, 1)
    _assert_full_schema(fresh_db)


def test_seed_round_trip(fresh_db: str, cfg: Config) -> None:
    """base → head → 001 → head → base → head: the same row every time it is present."""
    command.upgrade(cfg, "head")
    (first,) = _services(fresh_db)

    command.downgrade(cfg, "001")
    assert _services(fresh_db) == []
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    _assert_empty_schema(fresh_db)
    command.upgrade(cfg, "head")

    assert _current_revision(fresh_db) == _HEAD
    _assert_full_schema(fresh_db)
    (last,) = _services(fresh_db)
    assert {k: v for k, v in last.items() if k != "id"} == {
        k: v for k, v in first.items() if k != "id"
    }


# ── env.py ────────────────────────────────────────────────────────────────────


def test_postgres_scheme_alias_accepted(
    fresh_db: str,
    alembic_config: Callable[..., Config],
    seed_embedding_client: EmbeddingClient,
) -> None:
    """``postgres://`` works too — env.py hands the DSN to asyncpg unchanged."""
    dsn = urlunsplit(urlsplit(fresh_db)._replace(scheme="postgres"))
    command.upgrade(alembic_config(dsn, seed_embedding_client), "head")
    assert _current_revision(fresh_db) == _HEAD


def test_missing_database_url_is_a_clear_error(alembic_config: Callable[..., Config]) -> None:
    with pytest.raises(CommandError, match="DATABASE_URL is not set"):
        command.upgrade(alembic_config(""), "head")


def test_non_postgres_database_url_rejected(alembic_config: Callable[..., Config]) -> None:
    with pytest.raises(CommandError, match="not a postgres"):
        command.upgrade(alembic_config("sqlite:///./data/sentinel.db"), "head")


def test_offline_sql_contains_schema_and_guarded_grant(
    alembic_config: Callable[..., Config], capsys: pytest.CaptureFixture[str]
) -> None:
    """``alembic upgrade head --sql`` renders the DDL and the seed without a database."""
    command.upgrade(alembic_config("", EmbeddingClient(_ConstantModel(0.25))), "head", sql=True)
    out = capsys.readouterr().out

    assert "CREATE EXTENSION IF NOT EXISTS vector" in out
    for table in _TABLES:
        assert f"CREATE TABLE {table} (" in out
    for name in _EXPECTED_INDEXES:
        assert f"CREATE INDEX {name} ON" in out
    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gha-app') THEN" in out
    assert 'GRANT INSERT, SELECT ON deployments TO "gha-app";' in out

    # 002: the seed upsert with its embedding rendered as a literal, then the reindex.
    assert (
        "INSERT INTO services (name, team, tier, dependencies, runbook, metadata, embedding)" in out
    )
    assert "VALUES ('sentinel-watchtower', 'sentinel', 'standard', CAST('[]' AS jsonb)" in out
    assert "CAST('[" + ",".join(["0.25"] * 384) + "]' AS vector)" in out
    assert "ON CONFLICT (name) DO UPDATE SET" in out
    assert out.index("ON CONFLICT (name)") < out.index("REINDEX INDEX idx_services_embedding;")
    assert "UPDATE alembic_version SET version_num='002'" in out
