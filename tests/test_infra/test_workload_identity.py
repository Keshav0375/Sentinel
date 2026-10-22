"""Tests for sentinel.infra.workload_identity — the lazy credential and the Key Vault read.

Nothing here reaches Azure: DefaultAzureCredential and SecretClient are replaced by mocks.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
)

from sentinel.config import Settings
from sentinel.infra import workload_identity
from sentinel.infra.workload_identity import (
    KEY_VAULT_SECRETS,
    POSTGRES_ENTRA_SCOPE,
    LazyAzureCredential,
    entra_token_password,
    load_key_vault_secrets,
)

VAULT = "https://sentinel-kv-0375.vault.azure.net"

_VALUES = {
    "datadog-api-key": "dd-api-v",
    "datadog-app-key": "dd-app-v",
    "langfuse-public-key": "pk-v",
    "langfuse-secret-key": "sk-v",
    "langfuse-base-url": "https://kv.langfuse.example",
}

# ── Helpers ───────────────────────────────────────────────────────────────────


def _secret(value: str | None) -> MagicMock:
    return MagicMock(value=value)


def _vault(
    monkeypatch: pytest.MonkeyPatch, get_secret: Callable[[str], Any]
) -> tuple[MagicMock, MagicMock]:
    """Patch SecretClient with a context-manager client backed by *get_secret*."""
    client = MagicMock()
    client.get_secret.side_effect = get_secret
    client.__enter__.return_value = client  # like the real SecretClient, which returns self
    secret_client = MagicMock(return_value=client)
    monkeypatch.setattr(workload_identity, "SecretClient", secret_client)
    return secret_client, client


def _holder() -> tuple[LazyAzureCredential, MagicMock]:
    cred = MagicMock()
    return LazyAzureCredential(factory=lambda: cred), cred


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, key_vault_url=VAULT, **kwargs)  # type: ignore[call-arg]


def _all_values(name: str) -> MagicMock:
    return _secret(_VALUES[name])


def _logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr(workload_identity, "logger", mock)
    return mock


# ── LazyAzureCredential ───────────────────────────────────────────────────────


def test_credential_not_built_until_get() -> None:
    factory = MagicMock()
    holder = LazyAzureCredential(factory=factory)
    factory.assert_not_called()
    assert holder.built is False
    first, second = holder.get(), holder.get()
    factory.assert_called_once_with()
    assert first is second is factory.return_value
    assert holder.built is True


def test_credential_default_factory_is_default_azure_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = MagicMock()
    monkeypatch.setattr(workload_identity, "DefaultAzureCredential", default)
    assert LazyAzureCredential().get() is default.return_value
    default.assert_called_once_with()


def test_credential_close_without_build_is_noop() -> None:
    factory = MagicMock()
    LazyAzureCredential(factory=factory).close()
    factory.assert_not_called()


def test_credential_close_closes_once_and_swallows_errors() -> None:
    holder, cred = _holder()
    cred.close.side_effect = RuntimeError("boom")
    holder.get()
    holder.close()
    holder.close()
    cred.close.assert_called_once_with()
    assert holder.built is False


# ── entra_token_password ──────────────────────────────────────────────────────


async def test_entra_password_fetches_a_token_per_call_off_loop() -> None:
    threads: list[int] = []
    cred = MagicMock()

    def get_token(*_scopes: str) -> AccessToken:
        threads.append(threading.get_ident())
        return AccessToken(f"tok-{len(threads)}", 4102444800)

    cred.get_token.side_effect = get_token
    password = entra_token_password(cred)
    assert [await password(), await password()] == ["tok-1", "tok-2"]
    assert cred.get_token.call_count == 2
    cred.get_token.assert_called_with(POSTGRES_ENTRA_SCOPE)
    assert all(t != threading.get_ident() for t in threads)


# ── Key Vault ─────────────────────────────────────────────────────────────────


def test_key_vault_contract_names() -> None:
    assert dict(KEY_VAULT_SECRETS) == {
        "datadog-api-key": "dd_api_key",
        "datadog-app-key": "dd_app_key",
        "langfuse-public-key": "langfuse_public_key",
        "langfuse-secret-key": "langfuse_secret_key",
        "langfuse-base-url": "langfuse_base_url",
    }
    assert set(KEY_VAULT_SECRETS.values()).isdisjoint(
        {"anthropic_api_key", "openai_api_key", "github_api_token"}
    )


async def test_key_vault_skipped_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_client, _ = _vault(monkeypatch, _all_values)
    holder, _ = _holder()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    env: dict[str, str] = {}
    result = await load_key_vault_secrets(settings, holder, environ=env)
    assert result is settings
    assert holder.built is False
    secret_client.assert_not_called()
    assert env == {}


async def test_key_vault_maps_secrets_to_settings_and_langfuse_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_client, _ = _vault(monkeypatch, _all_values)
    holder, cred = _holder()
    env: dict[str, str] = {}
    result = await load_key_vault_secrets(_settings(), holder, environ=env)
    assert result.dd_api_key is not None and result.dd_api_key.get_secret_value() == "dd-api-v"
    assert result.dd_app_key is not None and result.dd_app_key.get_secret_value() == "dd-app-v"
    assert result.langfuse_public_key is not None
    assert result.langfuse_public_key.get_secret_value() == "pk-v"
    assert result.langfuse_secret_key is not None
    assert result.langfuse_secret_key.get_secret_value() == "sk-v"
    assert result.langfuse_base_url == "https://kv.langfuse.example"
    assert env == {
        "LANGFUSE_PUBLIC_KEY": "pk-v",
        "LANGFUSE_SECRET_KEY": "sk-v",
        "LANGFUSE_BASE_URL": "https://kv.langfuse.example",
    }
    secret_client.assert_called_once_with(vault_url=VAULT, credential=cred)


async def test_key_vault_value_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    def get_secret(name: str) -> MagicMock:
        if name == "datadog-api-key":
            return _secret("vault-dd")
        raise ResourceNotFoundError("nope")

    _vault(monkeypatch, get_secret)
    holder, _ = _holder()
    settings = _settings(dd_api_key="env-dd")
    result = await load_key_vault_secrets(settings, holder, environ={})
    assert result.dd_api_key is not None and result.dd_api_key.get_secret_value() == "vault-dd"


async def test_missing_secret_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    def get_secret(name: str) -> MagicMock:
        if name == "datadog-app-key":
            raise ResourceNotFoundError("nope")
        return _all_values(name)

    _vault(monkeypatch, get_secret)
    log = _logger(monkeypatch)
    holder, _ = _holder()
    result = await load_key_vault_secrets(_settings(), holder, environ={})
    assert result.dd_app_key is None
    assert result.dd_api_key is not None
    assert result.langfuse_public_key is not None
    assert result.langfuse_secret_key is not None
    assert result.langfuse_base_url == "https://kv.langfuse.example"
    log.warning.assert_any_call(
        "key_vault_secret_missing", secret="datadog-app-key", field="dd_app_key"
    )


async def test_blank_secret_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def get_secret(name: str) -> MagicMock:
        return _secret("  ") if name == "langfuse-base-url" else _all_values(name)

    _vault(monkeypatch, get_secret)
    holder, _ = _holder()
    env: dict[str, str] = {}
    result = await load_key_vault_secrets(_settings(), holder, environ=env)
    assert result.langfuse_base_url is None
    assert "LANGFUSE_BASE_URL" not in env


async def test_unreadable_secret_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    def get_secret(name: str) -> MagicMock:
        if name == "datadog-api-key":
            raise HttpResponseError(message="Forbidden")
        return _all_values(name)

    _, client = _vault(monkeypatch, get_secret)
    holder, _ = _holder()
    result = await load_key_vault_secrets(_settings(), holder, environ={})
    assert result.dd_api_key is None
    assert result.dd_app_key is not None
    assert result.langfuse_public_key is not None
    assert result.langfuse_secret_key is not None
    assert result.langfuse_base_url is not None
    assert client.get_secret.call_count == 5


async def test_auth_failure_stops_and_keeps_env_values(monkeypatch: pytest.MonkeyPatch) -> None:
    def get_secret(_name: str) -> MagicMock:
        raise ClientAuthenticationError("no identity")

    _, client = _vault(monkeypatch, get_secret)
    log = _logger(monkeypatch)
    holder, _ = _holder()
    env: dict[str, str] = {}
    settings = _settings(dd_api_key="env-dd")
    result = await load_key_vault_secrets(settings, holder, environ=env)
    assert client.get_secret.call_count == 1
    assert result.model_dump() == settings.model_dump()
    assert env == {}
    assert log.error.call_args.args[0] == "key_vault_unavailable"


async def test_bad_vault_url_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_client, _ = _vault(monkeypatch, _all_values)
    secret_client.side_effect = ValueError("bad url")
    log = _logger(monkeypatch)
    holder, _ = _holder()
    settings = _settings()
    result = await load_key_vault_secrets(settings, holder, environ={})
    assert result.model_dump() == settings.model_dump()
    assert log.error.call_args.args[0] == "key_vault_unavailable"


async def test_llm_keys_never_read_from_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    _, client = _vault(monkeypatch, lambda name: _secret(f"v-{name}"))
    holder, _ = _holder()
    settings = _settings(anthropic_api_key="sk-ant-env", openai_api_key="sk-env")
    result = await load_key_vault_secrets(settings, holder, environ={})
    assert [c.args[0] for c in client.get_secret.call_args_list] == list(KEY_VAULT_SECRETS)
    assert result.anthropic_api_key is not None
    assert result.anthropic_api_key.get_secret_value() == "sk-ant-env"
    assert result.openai_api_key is not None
    assert result.openai_api_key.get_secret_value() == "sk-env"


async def test_secret_values_never_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    _vault(monkeypatch, lambda name: _secret(f"SECRET-{name}-xyz"))
    log = _logger(monkeypatch)
    holder, _ = _holder()
    await load_key_vault_secrets(_settings(), holder, environ={})
    assert log.info.called
    assert "SECRET-" not in repr(log.mock_calls)
