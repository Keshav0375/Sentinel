"""Tests for sentinel.models.service."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentinel.models.service import ServiceMetadata, ServiceTier

# ── ServiceTier ───────────────────────────────────────────────────────────────


def test_service_tier_values() -> None:
    assert ServiceTier.CRITICAL == "critical"
    assert ServiceTier.STANDARD == "standard"
    assert ServiceTier.BEST_EFFORT == "best-effort"


def test_service_tier_from_string() -> None:
    assert ServiceTier("critical") is ServiceTier.CRITICAL
    assert ServiceTier("best-effort") is ServiceTier.BEST_EFFORT


def test_service_tier_invalid_raises() -> None:
    with pytest.raises(ValueError):
        ServiceTier("platinum")


# ── ServiceMetadata ───────────────────────────────────────────────────────────


def _api_gateway() -> ServiceMetadata:
    return ServiceMetadata(
        name="api-gateway",
        team="team-platform",
        tier=ServiceTier.CRITICAL,
    )


def test_service_metadata_required_fields() -> None:
    svc = _api_gateway()
    assert svc.name == "api-gateway"
    assert svc.team == "team-platform"
    assert svc.tier is ServiceTier.CRITICAL


def test_service_metadata_defaults() -> None:
    svc = _api_gateway()
    assert svc.oncall_channel is None
    assert svc.repo_url is None
    assert svc.description == ""
    assert svc.dependencies == []


def test_service_metadata_full() -> None:
    svc = ServiceMetadata(
        name="order-service",
        team="team-commerce",
        tier=ServiceTier.CRITICAL,
        oncall_channel="#oncall-commerce",
        repo_url="https://github.com/acme/order-service",
        description="Handles order creation and fulfilment",
        dependencies=["payment-service", "notification-service"],
    )
    assert svc.oncall_channel == "#oncall-commerce"
    assert "payment-service" in svc.dependencies
    assert len(svc.dependencies) == 2


def test_service_metadata_tier_string_coerced() -> None:
    svc = ServiceMetadata(name="cdn-proxy", team="team-platform", tier="standard")  # type: ignore[arg-type]
    assert svc.tier is ServiceTier.STANDARD


def test_service_metadata_invalid_tier_raises() -> None:
    with pytest.raises(ValidationError):
        ServiceMetadata(name="x", team="y", tier="gold")  # type: ignore[arg-type]


def test_service_metadata_missing_required_raises() -> None:
    with pytest.raises(ValidationError):
        ServiceMetadata(name="x")  # type: ignore[call-arg]


def test_service_metadata_json_round_trip() -> None:
    svc = ServiceMetadata(
        name="auth-service",
        team="team-identity",
        tier=ServiceTier.CRITICAL,
        dependencies=["user-service"],
    )
    reloaded = ServiceMetadata.model_validate_json(svc.model_dump_json())
    assert reloaded.name == svc.name
    assert reloaded.tier is ServiceTier.CRITICAL
    assert reloaded.dependencies == ["user-service"]
