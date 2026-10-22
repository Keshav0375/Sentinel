"""Inbound auth (§3.6): stateless Entra bearer validation against the identity tenant's v2 JWKS.

``require_incident_write`` is a FastAPI dependency. It checks the access token's RS256
signature (keys from the tenant's v2 JWKS), ``exp``, ``aud`` and ``iss`` (v2 only:
``https://login.microsoftonline.com/{tenant}/v2.0``; the v1 ``sts.windows.net`` issuer is
rejected), then the ``Incident.Write`` app role. ``aud`` is
checked against ``SENTINEL_API_AUDIENCE``, a comma-separated list: the API app's client-id
GUID (required; Entra v2 access tokens carry it) and optionally its identifier URI.
Required, no default.

- Any token problem, a missing or non-bearer header, an unreachable JWKS, or auth that is
  not configured → 401 ``invalid token`` with ``WWW-Authenticate: Bearer``.
- A valid token without the ``Incident.Write`` role → 403.

The contract fixes the dependency's signature, so the validator lives in module state set
by ``configure_auth(settings)``. The app lifespan (main.py) calls it at startup, which is
the clear ``auth_not_configured`` / ``auth_configured`` log, and wires the dependency onto
the routers. Until then the first protected request configures lazily from the
environment. Unset tenant or audience never stops boot: protected routes just return 401.

The token, its claims and the Authorization header are never logged.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import Any, Final, cast

import jwt
from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from sentinel.config import Settings, get_settings
from sentinel.infra.logging import get_logger

REQUIRED_ROLE = "Incident.Write"
JWKS_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
ISSUER_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/v2.0"  # v2 only
JWKS_CACHE_LIFESPAN_S = 3600.0
JWKS_FETCH_TIMEOUT_S = 10.0
INVALID_TOKEN = "invalid token"
MISSING_ROLE = "missing Incident.Write role"
NO_CLIENT_ID: Final = (
    "SENTINEL_API_AUDIENCE lists no client-id GUID (v2 tokens carry aud = the API app's client ID)"
)

logger = get_logger("sentinel.api.auth")

# auto_error=False so the 401 status and detail are ours; the scheme still registers in
# OpenAPI, which gives Swagger its Authorize button.
_BEARER = HTTPBearer(
    auto_error=False,
    bearerFormat="JWT",
    description=(
        "Entra v2 access token for the API app (aud = its client ID) carrying the "
        "Incident.Write app role."
    ),
)


def _is_guid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def parse_audiences(raw: str | None) -> tuple[str, ...]:
    """Split the comma-separated ``SENTINEL_API_AUDIENCE``.

    Entries are stripped, blanks dropped, a GUID entry is normalised to the lowercase
    hyphenated form Entra puts in ``aud``, and duplicates are dropped (first kept).
    """
    if raw is None:
        return ()
    seen: dict[str, None] = {}
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        if _is_guid(entry):
            entry = str(uuid.UUID(entry))
        seen.setdefault(entry)
    return tuple(seen)


def _build_jwks_client(tenant_id: str) -> jwt.PyJWKClient:
    """The tenant's v2 JWKS client. PyJWKClient caches the key set and refetches once on
    an unknown ``kid`` (key rotation), at most once per cooldown."""
    return jwt.PyJWKClient(
        JWKS_URL_TEMPLATE.format(tenant_id=tenant_id),
        cache_jwk_set=True,
        lifespan=JWKS_CACHE_LIFESPAN_S,
        timeout=JWKS_FETCH_TIMEOUT_S,
    )


class EntraBearerValidator:
    """Validates one tenant's v2 access tokens for a set of accepted audiences."""

    def __init__(
        self,
        tenant_id: str,
        audiences: Sequence[str],
        jwks_client: jwt.PyJWKClient | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.audiences: tuple[str, ...] = tuple(audiences)
        if not self.audiences:
            raise ValueError("at least one audience is required")
        self.issuer = ISSUER_TEMPLATE.format(tenant_id=tenant_id)
        self.jwks_client = jwks_client if jwks_client is not None else _build_jwks_client(tenant_id)

    async def decode(self, token: str) -> dict[str, Any]:
        """Verify *token* and return its claims.

        Raises:
            jwt.PyJWTError: The token is malformed, expired, mis-signed, or has the wrong
                audience or issuer, or the JWKS could not be fetched. Raised unchanged.
        """
        # get_signing_key_from_jwt may fetch the JWKS over blocking urllib: keep it off the loop.
        signing_key = await asyncio.to_thread(self.jwks_client.get_signing_key_from_jwt, token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=list(self.audiences),
            issuer=self.issuer,
            options={"require": ["exp", "aud", "iss"]},
        )


def auth_config_problems(settings: Settings) -> list[str]:
    """Why auth cannot be configured from *settings* (empty when it can)."""
    problems: list[str] = []
    tenant = settings.sentinel_api_tenant_id
    if tenant is None:
        problems.append("SENTINEL_API_TENANT_ID is not set")
    else:
        try:
            uuid.UUID(tenant)
        except ValueError:
            problems.append("SENTINEL_API_TENANT_ID is not a GUID")
    audiences = parse_audiences(settings.sentinel_api_audience)
    if not audiences:
        problems.append("SENTINEL_API_AUDIENCE is not set")
    elif not any(_is_guid(a) for a in audiences):
        problems.append(NO_CLIENT_ID)
    return problems


_validator: EntraBearerValidator | None = None
_configured: bool = False


def configure_auth(settings: Settings, jwks_client: jwt.PyJWKClient | None = None) -> bool:
    """Build the validator from *settings* and log the outcome; True when auth is usable.

    Incomplete config leaves no validator (every protected route returns 401) and never
    raises, so the app still boots.
    """
    global _validator, _configured
    problems = auth_config_problems(settings)
    tenant_raw = settings.sentinel_api_tenant_id
    audiences = parse_audiences(settings.sentinel_api_audience)
    if problems or tenant_raw is None:
        _validator = None
        logger.error(
            "auth_not_configured",
            problems=problems,
            effect="every protected route returns 401",
        )
    else:
        tenant = str(uuid.UUID(tenant_raw))  # Entra's iss uses the lowercase hyphenated form
        _validator = EntraBearerValidator(tenant, audiences, jwks_client)
        logger.info(
            "auth_configured",
            tenant_id=tenant,
            audiences=list(_validator.audiences),
            issuer=_validator.issuer,
        )
    _configured = True
    return _validator is not None


def reset_auth() -> None:
    """Forget the configured validator (tests); the next request reconfigures lazily."""
    global _validator, _configured
    _validator = None
    _configured = False


def _current_validator() -> EntraBearerValidator | None:
    """The validator, configuring from the environment on first use if the lifespan did not."""
    if not _configured:
        configure_auth(get_settings())
    return _validator


def _unauthorized(reason: str) -> HTTPException:
    """Log the rejection reason (never the token) and build the 401."""
    logger.warning("auth_rejected", reason=reason)
    return HTTPException(
        status_code=401, detail=INVALID_TOKEN, headers={"WWW-Authenticate": "Bearer"}
    )


async def require_incident_write(
    creds: HTTPAuthorizationCredentials | None = Security(_BEARER),
) -> dict[str, Any]:
    """Require a valid Entra bearer token with the ``Incident.Write`` role; return its claims."""
    if creds is None:
        raise _unauthorized("missing_credentials")
    validator = _current_validator()
    if validator is None:
        raise _unauthorized("not_configured")
    try:
        claims = await validator.decode(creds.credentials)
    except jwt.PyJWKClientConnectionError as exc:
        raise _unauthorized("jwks_unavailable") from exc
    except jwt.PyJWTError as exc:
        raise _unauthorized(type(exc).__name__) from exc
    roles: object = claims.get("roles")
    if not isinstance(roles, list) or REQUIRED_ROLE not in cast("list[object]", roles):
        logger.warning("auth_forbidden", reason="missing_role", required_role=REQUIRED_ROLE)
        raise HTTPException(status_code=403, detail=MISSING_ROLE)
    return claims
