"""Azure runtime access: the workload-identity credential and the startup Key Vault read.

backend.md §8.2 and the decision of 2026-10-19 ("Runtime config"). One
``DefaultAzureCredential`` per process (workload identity in the pod) is built lazily on
first use, so nothing here calls Azure at import or at a boot without ``KEY_VAULT_URL`` /
``PGHOST``. The vault is read once at startup; secret values are never logged.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Final

from azure.core.credentials import TokenCredential
from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from pydantic import SecretStr

from sentinel.config import Settings
from sentinel.infra.logging import get_logger

__all__ = [
    "KEY_VAULT_SECRETS",
    "LANGFUSE_ENV_VARS",
    "POSTGRES_ENTRA_SCOPE",
    "LazyAzureCredential",
    "entra_token_password",
    "load_key_vault_secrets",
]

logger = get_logger("sentinel.infra.workload_identity")

POSTGRES_ENTRA_SCOPE: Final = "https://ossrdbms-aad.database.windows.net/.default"

# Key Vault secret name → Settings field. No other name is ever requested: the LLM keys
# and the GitHub PAT stay environment-only.
KEY_VAULT_SECRETS: Final[Mapping[str, str]] = {
    "datadog-api-key": "dd_api_key",
    "datadog-app-key": "dd_app_key",
    "langfuse-public-key": "langfuse_public_key",
    "langfuse-secret-key": "langfuse_secret_key",
    "langfuse-base-url": "langfuse_base_url",
}

_SECRET_STR_FIELDS: Final = frozenset(
    {"dd_api_key", "dd_app_key", "langfuse_public_key", "langfuse_secret_key"}
)

# The langfuse v3 client reads only the process environment, so these are mirrored there.
LANGFUSE_ENV_VARS: Final[Mapping[str, str]] = {
    "langfuse_public_key": "LANGFUSE_PUBLIC_KEY",
    "langfuse_secret_key": "LANGFUSE_SECRET_KEY",
    "langfuse_base_url": "LANGFUSE_BASE_URL",
}


class LazyAzureCredential:
    """One DefaultAzureCredential (workload identity in the pod), built on first use."""

    def __init__(self, factory: Callable[[], DefaultAzureCredential] | None = None) -> None:
        self._factory = factory
        self._credential: DefaultAzureCredential | None = None

    @property
    def built(self) -> bool:
        return self._credential is not None

    def get(self) -> DefaultAzureCredential:
        if self._credential is None:
            # The module global is resolved here, so tests can monkeypatch it.
            factory = self._factory if self._factory is not None else DefaultAzureCredential
            self._credential = factory()
            logger.info("azure_credential_created")
        return self._credential

    def close(self) -> None:
        """Close the credential if it was built. Never raises."""
        credential = self._credential
        if credential is None:
            return
        try:
            credential.close()
        except Exception as exc:
            logger.warning("azure_credential_close_failed", error=type(exc).__name__)
        finally:
            self._credential = None


def entra_token_password(
    credential: TokenCredential, scope: str = POSTGRES_ENTRA_SCOPE
) -> Callable[[], Awaitable[str]]:
    """Return an async password provider that yields an Entra access token.

    asyncpg calls it for every new connection; azure-identity caches and refreshes the
    token, and the blocking fetch runs in a thread. The token is never logged.
    """

    async def _password() -> str:
        token = await asyncio.to_thread(credential.get_token, scope)
        return token.token

    return _password


def _fetch_secrets(vault_url: str, credential: TokenCredential) -> dict[str, str]:
    """Read the contract secrets (sync; runs in a thread). Returns {settings field: value}.

    The except order is load-bearing: ResourceNotFoundError and ClientAuthenticationError
    both subclass HttpResponseError.
    """
    found: dict[str, str] = {}
    try:
        client = SecretClient(vault_url=vault_url, credential=credential)
    except ValueError as exc:
        logger.error(
            "key_vault_unavailable",
            vault_url=vault_url,
            error=type(exc).__name__,
            effect="secrets come from the environment only",
        )
        return found
    with client:
        for name, field in KEY_VAULT_SECRETS.items():
            try:
                secret = client.get_secret(name)
            except ResourceNotFoundError:
                logger.warning("key_vault_secret_missing", secret=name, field=field)
                continue
            except (ClientAuthenticationError, ServiceRequestError) as exc:
                logger.error(
                    "key_vault_unavailable",
                    vault_url=vault_url,
                    error=type(exc).__name__,
                    effect="remaining secrets come from the environment",
                )
                break
            except HttpResponseError as exc:
                logger.warning(
                    "key_vault_secret_unreadable",
                    secret=name,
                    field=field,
                    status=exc.status_code,
                    error=type(exc).__name__,
                )
                continue
            except AzureError as exc:
                logger.error(
                    "key_vault_unavailable",
                    vault_url=vault_url,
                    error=type(exc).__name__,
                    effect="remaining secrets come from the environment",
                )
                break
            value = (secret.value or "").strip()
            if not value:
                logger.warning("key_vault_secret_missing", secret=name, field=field, reason="blank")
                continue
            found[field] = value
    return found


async def load_key_vault_secrets(
    settings: Settings,
    credential: LazyAzureCredential,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> Settings:
    """Overlay the Key Vault secrets onto *settings* (a vault value beats an env value).

    No ``KEY_VAULT_URL`` returns *settings* itself and never touches the credential. The
    three LangFuse values are also written to the environment, the only place the
    LangFuse client reads them. A failure is logged and never fatal.
    """
    if settings.key_vault_url is None:
        logger.info("key_vault_skipped", reason="KEY_VAULT_URL not set")
        return settings
    found = await asyncio.to_thread(_fetch_secrets, settings.key_vault_url, credential.get())
    env = os.environ if environ is None else environ
    for field, value in found.items():
        if field in LANGFUSE_ENV_VARS:
            env[LANGFUSE_ENV_VARS[field]] = value
    update: dict[str, SecretStr | str] = {
        field: SecretStr(value) if field in _SECRET_STR_FIELDS else value
        for field, value in found.items()
    }
    logger.info(
        "key_vault_loaded",
        loaded=sorted(found),
        missing=sorted(set(KEY_VAULT_SECRETS.values()) - set(found)),
    )
    return settings.model_copy(update=update)
