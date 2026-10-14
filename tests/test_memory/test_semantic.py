"""Tests for sentinel.memory.semantic.SemanticMemory — asyncpg + pgvector.

Tests taking ``mem`` run against a real pgvector container (TEST_DATABASE_URL, see
tests/conftest.py): this module migrates one throwaway database to head — so the 002
seed row ``sentinel-watchtower`` exists — removes every other ``services`` row before
each test and drops the database afterwards. They skip with a reason when
TEST_DATABASE_URL is not set (fail in CI).

The throwaway database has sequential scans disabled, so every symptom search in this
module is planned through the ``idx_services_embedding`` IVFFlat index.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from pgvector import Vector

from sentinel.exceptions import SentinelMemoryError
from sentinel.infra.database import create_pool
from sentinel.memory import semantic
from sentinel.memory.base import MemoryStore
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.memory.semantic import SemanticMemory, _parse_runbook_steps
from sentinel.models.memory import Runbook, SemanticRecord
from sentinel.models.service import ServiceMetadata, ServiceTier
from sentinel.tools.service_lookup import _build_service_response

DIMS = 384
SEED = "sentinel-watchtower"

# Symptom text for the real-model search: it names the seeded service and its runbook's
# vocabulary (deploy, rollback); the decoys below share none of it.
_SYMPTOM = "sentinel-watchtower health check failing after deploy, rollback"

# Test-only decoys (never seeded): unrelated services the symptom must not rank above.
_DECOYS = {
    "payments-api": "payments-api\nteam: billing\ntier: critical\n\nCard authorization and "
    "refunds. On a rising decline rate, inspect the card-network webhook queue.",
    "search-indexer": "search-indexer\nteam: discovery\ntier: best-effort\n\nRebuilds the "
    "product search index nightly. On indexing lag, restart the queue consumer.",
    "image-resizer": "image-resizer\nteam: media\ntier: standard\n\nResizes uploaded "
    "photos into thumbnails. On a growing backlog, scale out the worker pool.",
}


def _one_hot(dim: int) -> list[float]:
    vec = [0.0] * DIMS
    vec[dim] = 1.0
    return vec


# ── Database fixtures ─────────────────────────────────────────────────────────


def _with_database(dsn: str, name: str) -> str:
    return urlunsplit(urlsplit(dsn)._replace(path=f"/{name}"))


def _execute(dsn: str, sql: str) -> None:
    async def _go() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(_go())


@pytest.fixture(scope="module")
def _module_db() -> Iterator[dict[str, str]]:
    """Holder for this module's throwaway database; dropped when the module finishes."""
    holder: dict[str, str] = {}
    yield holder
    if holder:
        _execute(holder["server"], f'DROP DATABASE IF EXISTS "{holder["name"]}" WITH (FORCE)')


@pytest.fixture
def semantic_dsn(
    pg_dsn: str,
    _module_db: dict[str, str],
    alembic_config: Callable[..., Config],
    seed_embedding_client: EmbeddingClient,
) -> str:
    """A database at head (the §5.3 schema + the 002 seed row), extra services removed.

    Created and migrated on first use in this module. Sequential scans are off for the
    whole database, so the planner must serve symptom search from the IVFFlat index.
    """
    if not _module_db:
        name = f"sentinel_semantic_{uuid.uuid4().hex[:12]}"
        _execute(pg_dsn, f'CREATE DATABASE "{name}"')
        _execute(pg_dsn, f'ALTER DATABASE "{name}" SET enable_seqscan = off')
        _module_db.update(server=pg_dsn, name=name, dsn=_with_database(pg_dsn, name))
        command.upgrade(alembic_config(_module_db["dsn"], seed_embedding_client), "head")
    _execute(_module_db["dsn"], f"DELETE FROM services WHERE name <> '{SEED}'")
    return _module_db["dsn"]


@pytest.fixture
async def pool(semantic_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(semantic_dsn, min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def mem(pool: asyncpg.Pool) -> SemanticMemory:
    return SemanticMemory(pool)


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _insert_service(
    pool: asyncpg.Pool,
    name: str,
    *,
    team: str | None = "team-test",
    tier: str | None = "standard",
    dependencies: list[str] | None = None,
    runbook: str | None = None,
    metadata: dict[str, Any] | None = None,
    embedding: list[float] | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO services (name, team, tier, dependencies, runbook, metadata, embedding)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6::jsonb, $7)
            """,
            name,
            team,
            tier,
            None if dependencies is None else json.dumps(dependencies),
            runbook,
            None if metadata is None else json.dumps(metadata),
            None if embedding is None else Vector(embedding),
        )


async def _reindex(pool: asyncpg.Pool) -> None:
    """Retrain the IVFFlat lists on the current rows (as 002 does after its insert)."""
    async with pool.acquire() as conn:
        await conn.execute("REINDEX INDEX idx_services_embedding")


# ── Protocol compliance ───────────────────────────────────────────────────────


def test_semantic_memory_satisfies_protocol() -> None:
    instance = SemanticMemory.__new__(SemanticMemory)
    assert isinstance(instance, MemoryStore)


# ── No database configured ────────────────────────────────────────────────────


def _calls() -> dict[str, Callable[[SemanticMemory], Any]]:
    return {
        "get_service": lambda m: m.get_service(SEED),
        "get_dependencies": lambda m: m.get_dependencies(SEED),
        "get_runbook": lambda m: m.get_runbook(SEED, "bad_deploy"),
        "get_all_runbooks": lambda m: m.get_all_runbooks(SEED),
        "search_services": lambda m: m.search_services(_one_hot(0)),
        "query": lambda m: m.query(SEED),
        "get": lambda m: m.get(SEED),
    }


@pytest.mark.parametrize("method", list(_calls()))
async def test_every_lookup_raises_without_a_pool(method: str) -> None:
    """pool=None never yields empty results or KeyError: every lookup says unavailable."""
    no_db = SemanticMemory(None)
    with pytest.raises(SentinelMemoryError, match="memory unavailable"):
        await _calls()[method](no_db)
    assert no_db.service_cache_size == 0
    assert no_db.runbook_cache_size == 0


async def test_store_and_delete_stay_read_only_without_a_pool() -> None:
    no_db = SemanticMemory(None)
    with pytest.raises(NotImplementedError, match="read-only"):
        await no_db.store(object())
    with pytest.raises(NotImplementedError, match="read-only"):
        await no_db.delete(SEED)


async def test_database_failure_is_not_service_not_found(semantic_dsn: str) -> None:
    """A database that fails raises SentinelMemoryError — never KeyError or None."""
    p = await create_pool(semantic_dsn, min_size=1, max_size=1)
    await p.close()
    broken = SemanticMemory(p)
    with pytest.raises(SentinelMemoryError):
        await broken.get_service(SEED)
    with pytest.raises(SentinelMemoryError):
        await broken.get(SEED)


# ── Lookup by name (decision 2026-10-12, item 4 mapping) ──────────────────────


async def test_seeded_service_maps_to_service_metadata(mem: SemanticMemory) -> None:
    svc = await mem.get_service(SEED)
    assert svc == ServiceMetadata(
        name=SEED,
        team="sentinel",
        tier=ServiceTier.STANDARD,
        oncall_channel=None,
        repo_url="https://github.com/Keshav0375/Sentinel-deployment",
        description="",
        dependencies=[],
    )


async def test_metadata_keys_map_to_service_fields(mem: SemanticMemory, pool: asyncpg.Pool) -> None:
    await _insert_service(
        pool,
        "checkout",
        team="team-pay",
        tier="critical",
        dependencies=["payments-db", "fraud-api"],
        metadata={
            "oncall_channel": "#oncall-pay",
            "description": "Checkout flow",
            "repo_url": "https://git.example.com/pay/checkout",
            "repo": "acme/checkout",
        },
    )
    svc = await mem.get_service("checkout")
    assert svc.team == "team-pay"
    assert svc.tier is ServiceTier.CRITICAL
    assert svc.oncall_channel == "#oncall-pay"
    assert svc.description == "Checkout flow"
    # An explicit repo_url wins over the derived https://github.com/<repo>.
    assert svc.repo_url == "https://git.example.com/pay/checkout"
    assert svc.dependencies == ["payments-db", "fraud-api"]


async def test_null_columns_map_to_empty_defaults(mem: SemanticMemory, pool: asyncpg.Pool) -> None:
    await _insert_service(pool, "bare", team=None, tier="best-effort")
    svc = await mem.get_service("bare")
    assert svc.team == ""
    assert svc.tier is ServiceTier.BEST_EFFORT
    assert svc.oncall_channel is None
    assert svc.repo_url is None
    assert svc.description == ""
    assert svc.dependencies == []


async def test_invalid_tier_raises_memory_error(mem: SemanticMemory, pool: asyncpg.Pool) -> None:
    await _insert_service(pool, "odd-tier", tier="gold")
    with pytest.raises(SentinelMemoryError, match="invalid tier"):
        await mem.get_service("odd-tier")


async def test_null_tier_reads_as_standard_with_a_warning(
    mem: SemanticMemory, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = MagicMock()
    monkeypatch.setattr(semantic, "logger", log)
    await _insert_service(pool, "untiered", tier=None)

    svc = await mem.get_service("untiered")

    assert svc.tier is ServiceTier.STANDARD
    log.warning.assert_called_once()
    assert log.warning.call_args.kwargs["service"] == "untiered"


async def test_search_one_bad_row_never_fails_the_others(
    mem: SemanticMemory, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = MagicMock()
    monkeypatch.setattr(semantic, "logger", log)
    await _insert_service(pool, "good-svc", tier="critical", embedding=_one_hot(0))
    await _insert_service(pool, "untiered-svc", tier=None, embedding=_one_hot(1))
    await _insert_service(pool, "gold-svc", tier="gold", embedding=_one_hot(2))
    await _reindex(pool)

    query = [0.0] * DIMS
    query[0], query[1], query[2] = 0.6, 0.5, 0.4
    results = await mem.search_services(query, top_k=3)

    # NULL tier is served as standard; the invalid (non-NULL) tier row is skipped.
    assert [(svc.name, svc.tier) for svc, _ in results] == [
        ("good-svc", ServiceTier.CRITICAL),
        ("untiered-svc", ServiceTier.STANDARD),
    ]
    warned = {c.kwargs["service"] for c in log.warning.call_args_list}
    assert warned == {"untiered-svc", "gold-svc"}
    # get_service still reports the invalid tier rather than hiding it.
    with pytest.raises(SentinelMemoryError, match="invalid tier"):
        await mem.get_service("gold-svc")


async def test_unknown_service_raises_key_error(mem: SemanticMemory) -> None:
    with pytest.raises(KeyError, match="no-such-service"):
        await mem.get_service("no-such-service")


async def test_lookup_is_exact_by_name(mem: SemanticMemory) -> None:
    with pytest.raises(KeyError):
        await mem.get_service("SENTINEL-WATCHTOWER")
    with pytest.raises(KeyError):
        await mem.get_service("sentinel-watch%")


async def test_get_dependencies(mem: SemanticMemory, pool: asyncpg.Pool) -> None:
    await _insert_service(pool, "api", dependencies=["auth", "orders"])
    assert await mem.get_dependencies("api") == ["auth", "orders"]
    assert await mem.get_dependencies(SEED) == []


async def test_get_dependencies_unknown_raises_key_error(mem: SemanticMemory) -> None:
    with pytest.raises(KeyError):
        await mem.get_dependencies("no-such-service")


# ── Runbooks (services.runbook) ───────────────────────────────────────────────


async def test_seeded_runbook_gives_ordered_steps(mem: SemanticMemory) -> None:
    runbook = await mem.get_runbook(SEED, "bad_deploy")
    assert runbook is not None
    assert runbook.id == f"{SEED}-runbook"
    assert runbook.service_name == SEED
    assert runbook.failure_class == "bad_deploy"
    assert len(runbook.steps) == 3
    first, second, third = runbook.steps
    # The wrapped continuation line belongs to its numbered item.
    assert first.startswith("Identify the culprit merge:")
    assert first.endswith("(PR number and commit sha).")
    assert "just before the alert" in first
    assert second.startswith("Sentinel drafts a rollback spec")
    assert second.endswith("on Keshav0375/Sentinel-deployment.")
    assert third == (
        "A human reviews and merges the revert PR, which redeploys the previous version."
    )


async def test_get_runbook_ignores_failure_class_for_selection(mem: SemanticMemory) -> None:
    a = await mem.get_runbook(SEED, "bad_deploy")
    b = await mem.get_runbook(SEED, "memory_leak")
    assert a is not None and b is not None
    assert a.steps == b.steps
    assert (a.failure_class, b.failure_class) == ("bad_deploy", "memory_leak")


async def test_non_list_runbook_is_one_step(mem: SemanticMemory, pool: asyncpg.Pool) -> None:
    text = "# prose-svc\n\nRestart the service, then watch the error rate for ten minutes."
    await _insert_service(pool, "prose-svc", runbook=f"\n{text}\n\n")
    runbook = await mem.get_runbook("prose-svc", "error_rate")
    assert runbook is not None
    assert runbook.steps == [text]


async def test_bulleted_runbook_steps(mem: SemanticMemory, pool: asyncpg.Pool) -> None:
    await _insert_service(pool, "bullets", runbook="Steps:\n- drain\n* restart\n+ verify\n")
    runbook = await mem.get_runbook("bullets", "x")
    assert runbook is not None
    assert runbook.steps == ["drain", "restart", "verify"]


async def test_null_runbook_gives_none(mem: SemanticMemory, pool: asyncpg.Pool) -> None:
    await _insert_service(pool, "no-runbook", runbook=None)
    await _insert_service(pool, "blank-runbook", runbook="   \n")
    for name in ("no-runbook", "blank-runbook"):
        assert await mem.get_runbook(name, "bad_deploy") is None
        assert await mem.get_all_runbooks(name) == []


async def test_get_runbook_unknown_service_returns_none(mem: SemanticMemory) -> None:
    assert await mem.get_runbook("no-such-service", "bad_deploy") is None
    assert await mem.get_all_runbooks("no-such-service") == []


async def test_get_all_runbooks_is_the_one_runbook(mem: SemanticMemory) -> None:
    runbooks = await mem.get_all_runbooks(SEED)
    assert len(runbooks) == 1
    assert runbooks[0].id == f"{SEED}-runbook"
    assert runbooks[0].failure_class == "general"
    assert len(runbooks[0].steps) == 3


# ── Pure parser (no database) ─────────────────────────────────────────────────


def test_parse_numbered_items_with_continuations() -> None:
    md = "# t\n\n1. one\n   more one\n2) two\n\n3. three\n\ntrailing prose\n"
    assert _parse_runbook_steps(md) == ["one more one", "two", "three"]


def test_parse_nested_items_are_their_own_steps() -> None:
    assert _parse_runbook_steps("- a\n  - b\n- c") == ["a", "b", "c"]


def test_parse_no_items_is_whole_text() -> None:
    assert _parse_runbook_steps("  Just do the thing.\nThen stop.  ") == [
        "Just do the thing.\nThen stop."
    ]


# ── Caching ───────────────────────────────────────────────────────────────────


async def test_lookups_are_cached(mem: SemanticMemory, pool: asyncpg.Pool) -> None:
    assert (mem.service_cache_size, mem.runbook_cache_size) == (0, 0)
    first = await mem.get_service(SEED)
    assert (mem.service_cache_size, mem.runbook_cache_size) == (1, 1)

    # The row changes underneath; the cached object is returned without a query.
    async with pool.acquire() as conn:
        await conn.execute("UPDATE services SET team = 'changed' WHERE name = $1", SEED)
    try:
        assert await mem.get_service(SEED) is first
        runbook = await mem.get_runbook(SEED, "bad_deploy")
        assert runbook is not None and len(runbook.steps) == 3
        assert mem.service_cache_size == 1
    finally:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE services SET team = 'sentinel' WHERE name = $1", SEED)


async def test_missing_service_is_not_cached(mem: SemanticMemory, pool: asyncpg.Pool) -> None:
    with pytest.raises(KeyError):
        await mem.get_service("late")
    assert mem.service_cache_size == 0
    await _insert_service(pool, "late")
    assert (await mem.get_service("late")).name == "late"


# ── MemoryStore protocol ──────────────────────────────────────────────────────


async def test_query_returns_semantic_record(mem: SemanticMemory) -> None:
    record = await mem.query(SEED)
    assert isinstance(record, SemanticRecord)
    assert record.service.name == SEED
    assert [rb.id for rb in record.runbooks] == [f"{SEED}-runbook"]


async def test_query_unknown_raises_key_error(mem: SemanticMemory) -> None:
    with pytest.raises(KeyError):
        await mem.query("no-such-service")


async def test_get_returns_metadata_or_none(mem: SemanticMemory) -> None:
    svc = await mem.get(SEED)
    assert isinstance(svc, ServiceMetadata)
    assert await mem.get("no-such-service") is None


async def test_store_and_delete_raise_not_implemented(mem: SemanticMemory) -> None:
    with pytest.raises(NotImplementedError):
        await mem.store(Runbook(id="x", service_name="x", failure_class="x", steps=[]))
    with pytest.raises(NotImplementedError):
        await mem.delete(SEED)


# ── search_services (pgvector, IVFFlat) ───────────────────────────────────────


async def test_symptom_search_ranks_the_matching_service_first(
    mem: SemanticMemory, pool: asyncpg.Pool, seed_embedding_client: EmbeddingClient
) -> None:
    """Real model: the seeded service beats three decoys, read through the index."""
    for name, text in _DECOYS.items():
        await _insert_service(pool, name, embedding=await seed_embedding_client.embed(text))
    await _reindex(pool)

    results = await mem.search_services(await seed_embedding_client.embed(_SYMPTOM), top_k=4)

    names = [svc.name for svc, _ in results]
    assert names[0] == SEED
    assert sorted(names) == sorted([SEED, *_DECOYS])
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)
    assert results[0][0].repo_url == "https://github.com/Keshav0375/Sentinel-deployment"


async def test_search_ranks_by_cosine_similarity(mem: SemanticMemory, pool: asyncpg.Pool) -> None:
    for dim, name in enumerate(("db-svc", "mem-svc", "deploy-svc")):
        await _insert_service(pool, name, embedding=_one_hot(dim))
    await _insert_service(pool, "no-embedding")
    await _reindex(pool)

    # Mostly dim 2, a little dim 0: deploy-svc, then db-svc.
    query = [0.0] * DIMS
    query[2], query[0] = 0.9, 0.3
    results = await mem.search_services(query, top_k=2)

    assert [svc.name for svc, _ in results] == ["deploy-svc", "db-svc"]
    norm = (0.9**2 + 0.3**2) ** 0.5
    assert [score for _, score in results] == pytest.approx([0.9 / norm, 0.3 / norm], abs=1e-6)


async def test_search_returns_every_embedded_row_through_the_index(
    mem: SemanticMemory, pool: asyncpg.Pool
) -> None:
    """ivfflat.probes covers every list: no embedded row is lost, none without one appears."""
    for dim in range(12):
        await _insert_service(pool, f"svc-{dim}", embedding=_one_hot(dim))
    await _insert_service(pool, "no-embedding")
    await _reindex(pool)

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL ivfflat.probes = 5")
        plan = "\n".join(
            r[0]
            for r in await conn.fetch(
                "EXPLAIN SELECT name FROM services WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> $1 LIMIT 50",
                Vector(_one_hot(0)),
            )
        )
    assert "idx_services_embedding" in plan

    results = await mem.search_services(_one_hot(7), top_k=50)
    names = [svc.name for svc, _ in results]
    assert names[0] == "svc-7"
    assert sorted(names) == sorted([SEED, *(f"svc-{d}" for d in range(12))])


async def test_search_top_k_limits_results(mem: SemanticMemory, pool: asyncpg.Pool) -> None:
    for dim in range(3):
        await _insert_service(pool, f"svc-{dim}", embedding=_one_hot(dim))
    results = await mem.search_services(_one_hot(1), top_k=1)
    assert len(results) == 1
    assert results[0][0].name == "svc-1"
    assert results[0][1] == pytest.approx(1.0, abs=1e-6)


async def test_search_rejects_wrong_dimensions(mem: SemanticMemory) -> None:
    with pytest.raises(SentinelMemoryError):
        await mem.search_services([1.0, 0.0, 0.0])


async def test_search_rejects_non_positive_top_k(mem: SemanticMemory) -> None:
    with pytest.raises(ValueError, match="top_k"):
        await mem.search_services(_one_hot(0), top_k=0)


# ── get_service_metadata tool over real memory ────────────────────────────────


async def test_service_lookup_tool_reports_memory_unavailable() -> None:
    result = await _build_service_response(SemanticMemory(None), SEED)
    assert result.startswith("ERROR: memory unavailable")
    assert "no database configured" in result
    assert "not found" not in result


async def test_service_lookup_tool_reports_service_not_found(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, "no-such-service")
    assert result == "ERROR: Service 'no-such-service' not found in the service map."


async def test_service_lookup_tool_formats_the_seeded_service(mem: SemanticMemory) -> None:
    result = await _build_service_response(mem, SEED)
    assert f"name: {SEED}" in result
    assert "tier: standard" in result
    assert "repo_url: https://github.com/Keshav0375/Sentinel-deployment" in result
    assert "[general] Identify the culprit merge" in result
