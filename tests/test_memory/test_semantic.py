"""Tests for sentinel.memory.semantic.SemanticMemory."""

from __future__ import annotations

from pathlib import Path

import pytest

from data.seed import seed
from sentinel.memory.base import MemoryStore
from sentinel.memory.semantic import SemanticMemory
from sentinel.models.memory import Runbook, SemanticRecord
from sentinel.models.service import ServiceMetadata, ServiceTier


@pytest.fixture()
async def db_path(tmp_path: Path) -> Path:
    """Return path to a freshly seeded SQLite database."""
    path = tmp_path / "test_semantic.db"
    await seed(path, verbose=False)
    return path


@pytest.fixture()
def mem(db_path: Path) -> SemanticMemory:
    return SemanticMemory(db_path)


# ── Protocol compliance ───────────────────────────────────────────────────────


def test_semantic_memory_satisfies_protocol() -> None:
    mem = SemanticMemory.__new__(SemanticMemory)
    assert isinstance(mem, MemoryStore)


# ── get_service ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_service_returns_service_metadata(mem: SemanticMemory) -> None:
    svc = await mem.get_service("api-gateway")
    assert isinstance(svc, ServiceMetadata)
    assert svc.name == "api-gateway"


@pytest.mark.asyncio
async def test_get_service_team_populated(mem: SemanticMemory) -> None:
    svc = await mem.get_service("api-gateway")
    assert svc.team == "team-platform"


@pytest.mark.asyncio
async def test_get_service_tier_is_enum(mem: SemanticMemory) -> None:
    svc = await mem.get_service("api-gateway")
    assert svc.tier is ServiceTier.CRITICAL


@pytest.mark.asyncio
async def test_get_service_best_effort_tier(mem: SemanticMemory) -> None:
    svc = await mem.get_service("analytics-pipeline")
    assert svc.tier is ServiceTier.BEST_EFFORT


@pytest.mark.asyncio
async def test_get_service_standard_tier(mem: SemanticMemory) -> None:
    svc = await mem.get_service("notification-service")
    assert svc.tier is ServiceTier.STANDARD


@pytest.mark.asyncio
async def test_get_service_oncall_channel(mem: SemanticMemory) -> None:
    svc = await mem.get_service("payment-service")
    assert svc.oncall_channel == "#oncall-payments"


@pytest.mark.asyncio
async def test_get_service_repo_url(mem: SemanticMemory) -> None:
    svc = await mem.get_service("user-service")
    assert svc.repo_url is not None
    assert svc.repo_url.startswith("https://github.com/")


@pytest.mark.asyncio
async def test_get_service_not_found_raises_key_error(mem: SemanticMemory) -> None:
    with pytest.raises(KeyError, match="nonexistent-svc"):
        await mem.get_service("nonexistent-svc")


@pytest.mark.asyncio
async def test_get_service_all_8_services(mem: SemanticMemory) -> None:
    names = [
        "api-gateway",
        "user-service",
        "auth-service",
        "payment-service",
        "order-service",
        "notification-service",
        "analytics-pipeline",
        "cdn-proxy",
    ]
    for name in names:
        svc = await mem.get_service(name)
        assert svc.name == name


# ── get_dependencies ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_dependencies_api_gateway(mem: SemanticMemory) -> None:
    deps = await mem.get_dependencies("api-gateway")
    assert "auth-service" in deps
    assert "payment-service" in deps
    assert "order-service" in deps


@pytest.mark.asyncio
async def test_get_dependencies_leaf_service(mem: SemanticMemory) -> None:
    # user-service has no dependencies
    deps = await mem.get_dependencies("user-service")
    assert deps == []


@pytest.mark.asyncio
async def test_get_dependencies_payment_service(mem: SemanticMemory) -> None:
    deps = await mem.get_dependencies("payment-service")
    assert deps == []


@pytest.mark.asyncio
async def test_get_dependencies_order_service(mem: SemanticMemory) -> None:
    deps = await mem.get_dependencies("order-service")
    assert "payment-service" in deps
    assert "notification-service" in deps


@pytest.mark.asyncio
async def test_get_dependencies_returns_list(mem: SemanticMemory) -> None:
    deps = await mem.get_dependencies("auth-service")
    assert isinstance(deps, list)
    assert all(isinstance(d, str) for d in deps)


# ── get_runbook ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_runbook_returns_runbook(mem: SemanticMemory) -> None:
    rb = await mem.get_runbook("api-gateway", "bad_deploy")
    assert isinstance(rb, Runbook)
    assert rb.service_name == "api-gateway"
    assert rb.failure_class == "bad_deploy"


@pytest.mark.asyncio
async def test_get_runbook_steps_non_empty(mem: SemanticMemory) -> None:
    rb = await mem.get_runbook("api-gateway", "bad_deploy")
    assert rb is not None
    assert len(rb.steps) >= 3
    assert all(isinstance(s, str) for s in rb.steps)


@pytest.mark.asyncio
async def test_get_runbook_payment_downstream_outage(mem: SemanticMemory) -> None:
    rb = await mem.get_runbook("payment-service", "downstream_outage")
    assert rb is not None
    assert rb.service_name == "payment-service"


@pytest.mark.asyncio
async def test_get_runbook_missing_returns_none(mem: SemanticMemory) -> None:
    rb = await mem.get_runbook("api-gateway", "nonexistent_failure_class")
    assert rb is None


@pytest.mark.asyncio
async def test_get_runbook_unknown_service_returns_none(mem: SemanticMemory) -> None:
    rb = await mem.get_runbook("unknown-service", "bad_deploy")
    assert rb is None


@pytest.mark.asyncio
async def test_get_runbook_all_scenario_pairs(mem: SemanticMemory) -> None:
    # Every scenario's (affected_service, failure_class) must have a runbook
    pairs = [
        ("api-gateway", "bad_deploy"),
        ("user-service", "bad_deploy"),
        ("auth-service", "bad_deploy"),
        ("payment-service", "db_pool"),
        ("payment-service", "downstream_outage"),
        ("order-service", "db_pool"),
        ("analytics-pipeline", "memory_leak"),
        ("notification-service", "memory_leak"),
        ("user-service", "config_regression"),
        ("auth-service", "downstream_outage"),
    ]
    for service, fc in pairs:
        rb = await mem.get_runbook(service, fc)
        assert rb is not None, f"Missing runbook for ({service}, {fc})"


# ── get_all_runbooks ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_all_runbooks_returns_list(mem: SemanticMemory) -> None:
    runbooks = await mem.get_all_runbooks("api-gateway")
    assert isinstance(runbooks, list)
    assert len(runbooks) >= 1


@pytest.mark.asyncio
async def test_get_all_runbooks_all_belong_to_service(mem: SemanticMemory) -> None:
    runbooks = await mem.get_all_runbooks("payment-service")
    for rb in runbooks:
        assert rb.service_name == "payment-service"


# ── Caching ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_caches_after_first_call(mem: SemanticMemory) -> None:
    assert mem.service_cache_size == 0
    await mem.get_service("api-gateway")
    assert mem.service_cache_size == 1


@pytest.mark.asyncio
async def test_service_not_re_fetched_from_db(mem: SemanticMemory) -> None:
    # First call populates cache
    await mem.get_service("api-gateway")
    # Mutate the cache directly to verify second call returns cached object
    mem._service_cache["api-gateway"] = ServiceMetadata(
        name="api-gateway", team="mutated", tier=ServiceTier.STANDARD
    )
    svc2 = await mem.get_service("api-gateway")
    assert svc2.team == "mutated"  # returned from cache, not DB


@pytest.mark.asyncio
async def test_runbook_caches_after_first_call(mem: SemanticMemory) -> None:
    assert mem.runbook_cache_size == 0
    await mem.get_runbook("api-gateway", "bad_deploy")
    assert mem.runbook_cache_size == 1


@pytest.mark.asyncio
async def test_runbook_none_result_is_cached(mem: SemanticMemory) -> None:
    await mem.get_runbook("api-gateway", "nonexistent_class")
    assert mem.runbook_cache_size == 1
    # Second call should hit cache (None is cached)
    result = await mem.get_runbook("api-gateway", "nonexistent_class")
    assert result is None


@pytest.mark.asyncio
async def test_multiple_services_cached_independently(mem: SemanticMemory) -> None:
    await mem.get_service("api-gateway")
    await mem.get_service("user-service")
    await mem.get_service("auth-service")
    assert mem.service_cache_size == 3


# ── MemoryStore protocol methods ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_protocol_returns_service_metadata(mem: SemanticMemory) -> None:
    result = await mem.get("api-gateway")
    assert isinstance(result, ServiceMetadata)
    assert result.name == "api-gateway"


@pytest.mark.asyncio
async def test_get_protocol_returns_none_for_unknown(mem: SemanticMemory) -> None:
    result = await mem.get("no-such-service")
    assert result is None


@pytest.mark.asyncio
async def test_query_returns_semantic_record(mem: SemanticMemory) -> None:
    result = await mem.query("api-gateway")
    assert isinstance(result, SemanticRecord)
    assert result.service.name == "api-gateway"


@pytest.mark.asyncio
async def test_query_includes_runbooks(mem: SemanticMemory) -> None:
    result = await mem.query("payment-service")
    assert len(result.runbooks) >= 1
    for rb in result.runbooks:
        assert rb.service_name == "payment-service"


@pytest.mark.asyncio
async def test_store_raises_not_implemented(mem: SemanticMemory) -> None:
    with pytest.raises(NotImplementedError):
        await mem.store({"id": "x"})


@pytest.mark.asyncio
async def test_delete_raises_not_implemented(mem: SemanticMemory) -> None:
    with pytest.raises(NotImplementedError):
        await mem.delete("some-id")
