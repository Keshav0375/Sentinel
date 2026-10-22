"""Tests for sentinel.api.auth — Entra bearer validation (§3.6).

Tokens are signed with throwaway RSA keys and verified against an in-memory JWKS client
(``_FakeJWKS`` overrides ``fetch_data``), so no test touches the network.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from fastapi import Depends, FastAPI
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from sentinel.api import auth
from sentinel.api.auth import EntraBearerValidator, require_incident_write
from sentinel.config import Settings

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
IDENTIFIER_URI = f"api://{TENANT}/sentinel-backend-d1-dev"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
JWKS_URL = f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"

# ── Helpers ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def rsa_keys() -> dict[str, RSAPrivateKey]:
    return {
        name: rsa.generate_private_key(public_exponent=65537, key_size=2048)
        for name in ("k1", "k2", "rogue")
    }


def _jwk(private_key: RSAPrivateKey, kid: str) -> dict[str, Any]:
    public = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    return public | {"kid": kid, "use": "sig", "alg": "RS256"}


class _FakeJWKS(jwt.PyJWKClient):
    """PyJWKClient whose fetch is in-memory and counted; caching and rotation are the real code."""

    def __init__(self, keys: list[dict[str, Any]], cooldown_duration: float = 30) -> None:
        super().__init__(JWKS_URL, cooldown_duration=cooldown_duration)
        self.jwks: dict[str, Any] = {"keys": keys}
        self.fetches = 0
        self.fetch_threads: list[int] = []
        self.fail = False

    def fetch_data(self) -> Any:
        self.fetches += 1
        self.fetch_threads.append(threading.get_ident())
        if self.fail:
            raise jwt.PyJWKClientConnectionError("down")
        self._last_successful_fetch = time.monotonic()
        return self.jwks


def _token(key: Any, kid: str = "k1", alg: str = "RS256", **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "aud": CLIENT_ID,
        "iss": ISSUER,
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "sub": "sub-1",
        "tid": TENANT,
        "roles": ["Incident.Write"],
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def _settings(**kw: Any) -> Settings:
    kw.setdefault("sentinel_api_tenant_id", TENANT)
    kw.setdefault("sentinel_api_audience", CLIENT_ID)
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def _get(client: TestClient, token: str) -> Any:
    return client.get("/protected", headers={"Authorization": f"Bearer {token}"})


@pytest.fixture(autouse=True)
def _reset_auth(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("SENTINEL_API_TENANT_ID", raising=False)
    monkeypatch.delenv("SENTINEL_API_AUDIENCE", raising=False)
    auth.reset_auth()
    yield
    auth.reset_auth()


@pytest.fixture
def jwks(rsa_keys: dict[str, RSAPrivateKey]) -> _FakeJWKS:
    fake = _FakeJWKS([_jwk(rsa_keys["k1"], "k1")])
    assert auth.configure_auth(_settings(), fake) is True
    return fake


@pytest.fixture
def app(jwks: _FakeJWKS) -> FastAPI:
    app = FastAPI()

    @app.get("/protected")
    async def protected(
        claims: dict[str, Any] = Depends(require_incident_write),
    ) -> dict[str, Any]:
        return {"sub": claims["sub"]}

    @app.get("/open")
    async def open_route() -> dict[str, str]:
        return {"status": "ok"}

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ── Accepted / rejected tokens ────────────────────────────────────────────────


def test_valid_token_returns_claims(
    client: TestClient, jwks: _FakeJWKS, rsa_keys: dict[str, RSAPrivateKey]
) -> None:
    resp = _get(client, _token(rsa_keys["k1"]))
    assert resp.status_code == 200
    assert resp.json() == {"sub": "sub-1"}
    assert jwks.fetches == 1


def test_missing_header_is_401(client: TestClient) -> None:
    resp = client.get("/protected")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid token"
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_non_bearer_scheme_is_401(client: TestClient) -> None:
    resp = client.get("/protected", headers={"Authorization": "Basic Zm9vOmJhcg=="})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid token"


@pytest.mark.parametrize("bad", ["not-a-jwt", "a.b.c", ""])
def test_malformed_token_is_401(client: TestClient, bad: str) -> None:
    resp = _get(client, bad)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid token"


def test_expired_token_is_401(client: TestClient, rsa_keys: dict[str, RSAPrivateKey]) -> None:
    now = int(time.time())
    token = _token(rsa_keys["k1"], exp=now - 600, iat=now - 4200, nbf=now - 4200)
    assert _get(client, token).status_code == 401


def test_token_without_exp_is_401(client: TestClient, rsa_keys: dict[str, RSAPrivateKey]) -> None:
    assert _get(client, _token(rsa_keys["k1"], exp=None)).status_code == 401


@pytest.mark.parametrize(
    "aud",
    [
        "api://sentinel-backend",
        f"api://{TENANT}/sentinel-backend-d2-dev",
        "99999999-9999-4999-8999-999999999999",
        None,
    ],
)
def test_wrong_audience_is_401(
    client: TestClient, rsa_keys: dict[str, RSAPrivateKey], aud: str | None
) -> None:
    assert _get(client, _token(rsa_keys["k1"], aud=aud)).status_code == 401


def test_client_id_in_upper_case_config_matches(
    client: TestClient, rsa_keys: dict[str, RSAPrivateKey]
) -> None:
    auth.configure_auth(
        _settings(sentinel_api_audience=CLIENT_ID.upper()), _FakeJWKS([_jwk(rsa_keys["k1"], "k1")])
    )
    assert _get(client, _token(rsa_keys["k1"], aud=CLIENT_ID)).status_code == 200


def test_identifier_uri_passes_when_listed(
    client: TestClient, rsa_keys: dict[str, RSAPrivateKey]
) -> None:
    auth.configure_auth(
        _settings(sentinel_api_audience=f"{CLIENT_ID}, {IDENTIFIER_URI}"),
        _FakeJWKS([_jwk(rsa_keys["k1"], "k1")]),
    )
    assert _get(client, _token(rsa_keys["k1"], aud=IDENTIFIER_URI)).status_code == 200
    assert _get(client, _token(rsa_keys["k1"], aud=CLIENT_ID)).status_code == 200


def test_identifier_uri_is_401_when_not_listed(
    client: TestClient, rsa_keys: dict[str, RSAPrivateKey]
) -> None:
    assert _get(client, _token(rsa_keys["k1"], aud=IDENTIFIER_URI)).status_code == 401


@pytest.mark.parametrize(
    "iss",
    [
        f"https://sts.windows.net/{TENANT}/",
        "https://login.microsoftonline.com/99999999-9999-9999-9999-999999999999/v2.0",
        None,
    ],
)
def test_wrong_issuer_is_401(
    client: TestClient, rsa_keys: dict[str, RSAPrivateKey], iss: str | None
) -> None:
    assert _get(client, _token(rsa_keys["k1"], iss=iss)).status_code == 401


def test_signature_from_unknown_key_is_401(
    client: TestClient, rsa_keys: dict[str, RSAPrivateKey]
) -> None:
    """Signed by a key the JWKS does not hold, though the header claims kid k1."""
    assert _get(client, _token(rsa_keys["rogue"], kid="k1")).status_code == 401


def test_hs256_token_is_401(client: TestClient) -> None:
    token = _token("x" * 64, kid="k1", alg="HS256")
    assert _get(client, token).status_code == 401


# ── Roles ─────────────────────────────────────────────────────────────────────


def test_no_roles_is_403(client: TestClient, rsa_keys: dict[str, RSAPrivateKey]) -> None:
    resp = _get(client, _token(rsa_keys["k1"], roles=None))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "missing Incident.Write role"


def test_wrong_role_is_403(client: TestClient, rsa_keys: dict[str, RSAPrivateKey]) -> None:
    assert _get(client, _token(rsa_keys["k1"], roles=["Incident.Read"])).status_code == 403


def test_roles_string_is_403(client: TestClient, rsa_keys: dict[str, RSAPrivateKey]) -> None:
    assert _get(client, _token(rsa_keys["k1"], roles="Incident.Write")).status_code == 403


# ── JWKS caching, rotation, failure ───────────────────────────────────────────


def test_key_rotation_refetches_once(rsa_keys: dict[str, RSAPrivateKey], app: FastAPI) -> None:
    fake = _FakeJWKS([_jwk(rsa_keys["k1"], "k1")], cooldown_duration=0)
    auth.configure_auth(_settings(), fake)
    client = TestClient(app)

    assert _get(client, _token(rsa_keys["k1"])).status_code == 200
    assert fake.fetches == 1

    fake.jwks = {"keys": [_jwk(rsa_keys["k2"], "k2")]}
    assert _get(client, _token(rsa_keys["k2"], kid="k2")).status_code == 200
    assert fake.fetches == 2

    assert _get(client, _token(rsa_keys["k2"], kid="k2")).status_code == 200
    assert fake.fetches == 2  # served from the cache


def test_unknown_kid_within_cooldown_does_not_refetch(
    client: TestClient, jwks: _FakeJWKS, rsa_keys: dict[str, RSAPrivateKey]
) -> None:
    assert _get(client, _token(rsa_keys["k1"])).status_code == 200
    assert jwks.fetches == 1

    assert _get(client, _token(rsa_keys["k2"], kid="k2")).status_code == 401
    assert jwks.fetches == 1


def test_jwks_unreachable_is_401(
    client: TestClient,
    jwks: _FakeJWKS,
    rsa_keys: dict[str, RSAPrivateKey],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = MagicMock()
    monkeypatch.setattr(auth, "logger", logger)
    jwks.fail = True

    assert _get(client, _token(rsa_keys["k1"])).status_code == 401
    logger.warning.assert_any_call("auth_rejected", reason="jwks_unavailable")


async def test_jwks_fetch_runs_off_event_loop(
    jwks: _FakeJWKS, rsa_keys: dict[str, RSAPrivateKey]
) -> None:
    loop_thread = threading.get_ident()
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=_token(rsa_keys["k1"]))

    claims = await auth.require_incident_write(creds)

    assert claims["sub"] == "sub-1"
    assert jwks.fetch_threads
    assert jwks.fetch_threads[0] != loop_thread


# ── Configuration ─────────────────────────────────────────────────────────────


def test_not_configured_logs_and_401s(
    client: TestClient, rsa_keys: dict[str, RSAPrivateKey], monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = MagicMock()
    monkeypatch.setattr(auth, "logger", logger)

    assert auth.configure_auth(Settings(_env_file=None)) is False  # type: ignore[call-arg]

    logger.error.assert_called_once_with(
        "auth_not_configured",
        problems=["SENTINEL_API_TENANT_ID is not set", "SENTINEL_API_AUDIENCE is not set"],
        effect="every protected route returns 401",
    )
    assert _get(client, _token(rsa_keys["k1"])).status_code == 401


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ()),
        ("", ()),
        (" , ", ()),
        (f" {CLIENT_ID.upper()} , ,{IDENTIFIER_URI}", (CLIENT_ID, IDENTIFIER_URI)),
        (f"{CLIENT_ID},{CLIENT_ID}", (CLIENT_ID,)),
    ],
)
def test_parse_audiences(raw: str | None, expected: tuple[str, ...]) -> None:
    assert auth.parse_audiences(raw) == expected


def test_audience_without_client_id_is_not_configured(
    client: TestClient, rsa_keys: dict[str, RSAPrivateKey]
) -> None:
    settings = _settings(sentinel_api_audience=IDENTIFIER_URI)

    assert auth.configure_auth(settings) is False
    assert auth.auth_config_problems(settings) == [auth.NO_CLIENT_ID]
    assert _get(client, _token(rsa_keys["k1"], aud=IDENTIFIER_URI)).status_code == 401


def test_tenant_not_a_guid_is_not_configured() -> None:
    settings = _settings(sentinel_api_tenant_id="contoso.onmicrosoft.com")

    assert auth.configure_auth(settings) is False
    assert auth.auth_config_problems(settings) == ["SENTINEL_API_TENANT_ID is not a GUID"]


def test_configure_logs_v2_issuer_and_normalises_tenant(
    jwks: _FakeJWKS, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = MagicMock()
    monkeypatch.setattr(auth, "logger", logger)

    assert auth.configure_auth(_settings(sentinel_api_tenant_id=TENANT.upper()), jwks) is True

    logger.info.assert_called_once_with(
        "auth_configured", tenant_id=TENANT, audiences=[CLIENT_ID], issuer=ISSUER
    )


def test_default_jwks_client_uses_v2_keys_url() -> None:
    validator = EntraBearerValidator(TENANT, [CLIENT_ID])  # builds the client; no request is made
    assert validator.jwks_client.uri == JWKS_URL
    assert validator.issuer == ISSUER


def test_validator_requires_an_audience() -> None:
    with pytest.raises(ValueError, match="audience"):
        EntraBearerValidator(TENANT, [], MagicMock())


def test_lazy_config_from_env(
    client: TestClient, rsa_keys: dict[str, RSAPrivateKey], monkeypatch: pytest.MonkeyPatch
) -> None:
    auth.reset_auth()
    monkeypatch.setenv("SENTINEL_API_TENANT_ID", TENANT)
    monkeypatch.setenv("SENTINEL_API_AUDIENCE", CLIENT_ID)
    monkeypatch.setattr(
        auth, "_build_jwks_client", lambda tid: _FakeJWKS([_jwk(rsa_keys["k1"], "k1")])
    )

    assert _get(client, _token(rsa_keys["k1"])).status_code == 200


def test_lazy_unconfigured_env_is_401(
    client: TestClient, rsa_keys: dict[str, RSAPrivateKey], monkeypatch: pytest.MonkeyPatch
) -> None:
    auth.reset_auth()
    monkeypatch.delenv("SENTINEL_API_TENANT_ID", raising=False)
    monkeypatch.delenv("SENTINEL_API_AUDIENCE", raising=False)

    assert _get(client, _token(rsa_keys["k1"])).status_code == 401


# ── Logging, routing, OpenAPI ─────────────────────────────────────────────────


def test_rejection_never_logs_token(
    client: TestClient, rsa_keys: dict[str, RSAPrivateKey], monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = MagicMock()
    monkeypatch.setattr(auth, "logger", logger)
    now = int(time.time())
    token = _token(rsa_keys["k1"], exp=now - 600, iat=now - 4200, nbf=now - 4200)

    assert _get(client, token).status_code == 401

    assert logger.warning.called
    assert token not in repr(logger.mock_calls)


def test_open_route_needs_no_token(client: TestClient) -> None:
    resp = client.get("/open")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_openapi_declares_bearer_scheme(app: FastAPI) -> None:
    spec = app.openapi()
    scheme = spec["components"]["securitySchemes"]["HTTPBearer"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert scheme["bearerFormat"] == "JWT"
    assert spec["paths"]["/protected"]["get"]["security"] == [{"HTTPBearer": []}]
    assert "security" not in spec["paths"]["/open"]["get"]
