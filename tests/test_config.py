"""Tests for sentinel.config — Settings validation and defaults.

No secret is required to boot (decision 2026-10-14): every credential is an optional
``SecretStr``, masked in repr and logs, and checked only where a feature uses it.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from sentinel import main
from sentinel.config import Settings, get_settings, secret_value

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

_SECRET_FIELDS = (
    "openai_api_key",
    "anthropic_api_key",
    "dd_api_key",
    "dd_app_key",
    "github_api_token",
    "langfuse_public_key",
    "langfuse_secret_key",
)

# Every env var Settings reads, plus LangFuse's deprecated host var.
_SETTINGS_ENV_VARS = tuple(name.upper() for name in Settings.model_fields) + ("LANGFUSE_HOST",)


@pytest.fixture
def no_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every env var Settings reads (litellm may have loaded a developer .env)."""
    for var in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_overrides_applied() -> None:
    s = Settings(
        sentinel_primary_provider="openai",
        sentinel_max_tool_calls=10,
        sentinel_log_level="DEBUG",
        sentinel_db_path=Path("/tmp/test.db"),
    )
    assert s.sentinel_primary_provider == "openai"
    assert s.sentinel_max_tool_calls == 10
    assert s.sentinel_log_level == "DEBUG"
    assert s.sentinel_db_path == Path("/tmp/test.db")


def test_max_tool_calls_zero_raises() -> None:
    with pytest.raises(Exception):
        Settings(sentinel_max_tool_calls=0)


@pytest.mark.parametrize("value", [21, 100])
def test_max_tool_calls_above_the_4_6_cap_raises(value: int) -> None:
    """§4.6 is a hard cap of 20: the setting can lower it, never raise it."""
    with pytest.raises(ValueError, match="<= 20"):
        Settings(sentinel_max_tool_calls=value)


@pytest.mark.parametrize("value", [1, 20])
def test_max_tool_calls_bounds_are_accepted(value: int) -> None:
    assert Settings(sentinel_max_tool_calls=value).sentinel_max_tool_calls == value


def test_invalid_log_level_raises() -> None:
    with pytest.raises(Exception):
        Settings(sentinel_log_level="VERBOSE")  # type: ignore[arg-type]


# ── Defaults ──────────────────────────────────────────────────────────────────


def test_max_tool_calls_defaults_to_20(no_settings_env: None) -> None:
    assert Settings(_env_file=None).sentinel_max_tool_calls == 20  # type: ignore[call-arg]


def test_dd_site_defaults_to_us5(no_settings_env: None) -> None:
    assert Settings(_env_file=None).dd_site == "us5.datadoghq.com"  # type: ignore[call-arg]


def test_fake_llm_defaults_off_and_reads_env(
    no_settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert Settings(_env_file=None).sentinel_fake_llm is False  # type: ignore[call-arg]
    monkeypatch.setenv("SENTINEL_FAKE_LLM", "1")
    assert Settings(_env_file=None).sentinel_fake_llm is True  # type: ignore[call-arg]


def test_new_fields_read_their_env_vars(
    no_settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DD_*, GITHUB_API_TOKEN and LANGFUSE_* map to the fields the tools and tracing use."""
    env = {
        "DD_API_KEY": "dd-api",
        "DD_APP_KEY": "dd-app",
        "DD_SITE": "datadoghq.eu",
        "GITHUB_API_TOKEN": "github_pat_x",
        "LANGFUSE_PUBLIC_KEY": "pk-lf-x",
        "LANGFUSE_SECRET_KEY": "sk-lf-x",
        "LANGFUSE_BASE_URL": "https://us.cloud.langfuse.com",
    }
    for var, value in env.items():
        monkeypatch.setenv(var, value)

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert secret_value(s.dd_api_key, "DD_API_KEY") == "dd-api"
    assert secret_value(s.dd_app_key, "DD_APP_KEY") == "dd-app"
    assert s.dd_site == "datadoghq.eu"
    assert secret_value(s.github_api_token, "GITHUB_API_TOKEN") == "github_pat_x"
    assert secret_value(s.langfuse_public_key, "LANGFUSE_PUBLIC_KEY") == "pk-lf-x"
    assert secret_value(s.langfuse_secret_key, "LANGFUSE_SECRET_KEY") == "sk-lf-x"
    assert s.langfuse_base_url == "https://us.cloud.langfuse.com"


def test_safety_limit_defaults(no_settings_env: None) -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.sentinel_max_concurrent_pipelines == 5
    assert s.sentinel_shutdown_grace_s == 20.0


def test_safety_limits_read_env(no_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_MAX_CONCURRENT_PIPELINES", "2")
    monkeypatch.setenv("SENTINEL_SHUTDOWN_GRACE_S", "0")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.sentinel_max_concurrent_pipelines == 2
    assert s.sentinel_shutdown_grace_s == 0.0


@pytest.mark.parametrize(
    ("field", "value"),
    [("sentinel_max_concurrent_pipelines", 0), ("sentinel_shutdown_grace_s", -1)],
)
def test_safety_limits_reject_bad_values(field: str, value: float) -> None:
    with pytest.raises(ValidationError, match=field):
        Settings(_env_file=None, **{field: value})  # type: ignore[arg-type]


def test_no_api_token_setting() -> None:
    """Inbound auth is Entra (§3.6): there is no shared-token setting."""
    assert "sentinel_api_token" not in Settings.model_fields


# ── Sample env ────────────────────────────────────────────────────────────────


def test_sample_env_loads(no_settings_env: None) -> None:
    """The repo's .env.example parses into Settings, inline comments and blanks included."""
    s = Settings(_env_file=_ENV_EXAMPLE)  # type: ignore[call-arg]

    assert s.sentinel_max_tool_calls == 20
    assert s.dd_site == "us5.datadoghq.com"
    assert s.sentinel_log_level == "INFO"
    assert s.sentinel_embedding_model == "all-MiniLM-L6-v2"
    assert (s.sentinel_db_pool_min, s.sentinel_db_pool_max) == (2, 10)
    assert s.sentinel_max_concurrent_pipelines == 5
    assert s.sentinel_shutdown_grace_s == 20.0
    assert s.database_url == ""
    assert s.langfuse_base_url == "https://cloud.langfuse.com"
    assert secret_value(s.anthropic_api_key, "ANTHROPIC_API_KEY") == "sk-ant-..."
    assert secret_value(s.openai_api_key, "OPENAI_API_KEY") == "sk-..."
    # Blank placeholders (`KEY=` with or without an inline comment) are unset.
    assert s.sentinel_primary_provider == "anthropic"
    for field in ("dd_api_key", "dd_app_key", "github_api_token"):
        assert getattr(s, field) is None, field
    assert s.sentinel_api_tenant_id is None and s.sentinel_api_audience is None
    assert s.key_vault_url is None and (s.pghost, s.pgdatabase, s.pguser) == (None, None, None)
    assert s.sentinel_dev_open_dashboard is False


def test_sample_env_has_no_api_token() -> None:
    text = _ENV_EXAMPLE.read_text()
    assert "SENTINEL_API_TOKEN" not in text
    assert "X-Sentinel-Token" not in text


# ── Secrets: optional, SecretStr, masked ──────────────────────────────────────


def test_boot_with_no_secrets_at_all(no_settings_env: None) -> None:
    """Settings constructs with no credential anywhere; every secret is None."""
    s = get_settings()  # conftest disables .env, so this is the bare environment
    for field in _SECRET_FIELDS:
        assert getattr(s, field) is None, field
    assert s.langfuse_base_url is None
    assert s.database_url == ""


def test_llm_keys_optional(no_settings_env: None) -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (s.anthropic_api_key, s.openai_api_key) == (None, None)
    assert Settings(anthropic_api_key="sk-ant-x").anthropic_api_key == SecretStr("sk-ant-x")


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_secret_counts_as_unset(
    blank: str, no_settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for field in _SECRET_FIELDS:
        monkeypatch.setenv(field.upper(), blank)
    monkeypatch.setenv("LANGFUSE_BASE_URL", blank)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    for field in _SECRET_FIELDS:
        assert getattr(s, field) is None, field
    assert s.langfuse_base_url is None


def test_api_auth_settings_default_none(no_settings_env: None) -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.sentinel_api_tenant_id is None
    assert s.sentinel_api_audience is None


def test_api_auth_settings_read_env(no_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_API_TENANT_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("SENTINEL_API_AUDIENCE", "api://x/sentinel-backend-d1-dev")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.sentinel_api_tenant_id == "11111111-2222-3333-4444-555555555555"
    assert s.sentinel_api_audience == "api://x/sentinel-backend-d1-dev"
    # Public identifiers, not secrets: plain str.
    assert type(s.sentinel_api_tenant_id) is str
    assert type(s.sentinel_api_audience) is str


@pytest.mark.parametrize("blank", ["", "   "])
def test_api_auth_blank_counts_as_unset(
    blank: str, no_settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTINEL_API_TENANT_ID", blank)
    monkeypatch.setenv("SENTINEL_API_AUDIENCE", blank)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.sentinel_api_tenant_id is None
    assert s.sentinel_api_audience is None


def test_runtime_access_fields_read_env(
    no_settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KEY_VAULT_URL, PG* and the dev flag map onto Settings; blank strings count as unset."""
    monkeypatch.setenv("KEY_VAULT_URL", "https://sentinel-kv-0375.vault.azure.net")
    monkeypatch.setenv("PGHOST", "sentinel-pg-0375.postgres.database.azure.com")
    monkeypatch.setenv("PGDATABASE", "sentinel")
    monkeypatch.setenv("PGUSER", "sentinel-backend-wi")
    monkeypatch.setenv("SENTINEL_DEV_OPEN_DASHBOARD", "1")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.key_vault_url == "https://sentinel-kv-0375.vault.azure.net"
    assert s.pghost == "sentinel-pg-0375.postgres.database.azure.com"
    assert s.pgdatabase == "sentinel"
    assert s.pguser == "sentinel-backend-wi"
    assert s.sentinel_dev_open_dashboard is True

    for var in ("KEY_VAULT_URL", "PGHOST", "PGDATABASE", "PGUSER"):
        monkeypatch.setenv(var, "  ")
    blank = Settings(_env_file=None)  # type: ignore[call-arg]
    assert blank.key_vault_url is None
    assert (blank.pghost, blank.pgdatabase, blank.pguser) == (None, None, None)


def test_secrets_are_secretstr_and_masked() -> None:
    values = {field: f"leak-{field}-value" for field in _SECRET_FIELDS}
    s = Settings(**values)  # type: ignore[arg-type]

    for field in _SECRET_FIELDS:
        assert isinstance(getattr(s, field), SecretStr), field

    rendered = [repr(s), str(s), f"{s}", s.model_dump_json(), repr(s.model_dump())]
    for text in rendered:
        for value in values.values():
            assert value not in text
    assert "**********" in repr(s)


def test_secret_value_reveals_or_names_missing_env_var() -> None:
    assert secret_value(SecretStr("abc"), "DD_API_KEY") == "abc"
    with pytest.raises(ValueError, match="DD_APP_KEY is not set"):
        secret_value(None, "DD_APP_KEY")


def test_primary_provider_reads_env_and_rejects_others(
    no_settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4.7: SENTINEL_PRIMARY_PROVIDER is 'anthropic' (default) or 'openai', nothing else."""
    assert Settings(_env_file=None).sentinel_primary_provider == "anthropic"  # type: ignore[call-arg]
    monkeypatch.setenv("SENTINEL_PRIMARY_PROVIDER", "openai")
    assert Settings(_env_file=None).sentinel_primary_provider == "openai"  # type: ignore[call-arg]
    monkeypatch.setenv("SENTINEL_PRIMARY_PROVIDER", "groq")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_removed_provider_and_model_settings_are_gone() -> None:
    """Groq/Azure/Gemini settings and per-env model overrides are removed (decision 2026-10-15)."""
    removed = {
        "groq_api_key",
        "groq_base_url",
        "azure_api_key",
        "azure_api_base",
        "azure_api_version",
        "gemini_api_key",
        "sentinel_triage_model",
        "sentinel_analysis_model",
        "sentinel_judge_model",
    }
    assert not removed & set(Settings.model_fields)


def test_model_env_vars_are_ignored(no_settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale SENTINEL_*_MODEL in an env file neither fails boot nor repoints a model."""
    monkeypatch.setenv("SENTINEL_TRIAGE_MODEL", "groq/llama-3.1-8b-instant")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert not hasattr(s, "sentinel_triage_model")


# ── PostgreSQL pool settings ──────────────────────────────────────────────────


def test_db_pool_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DATABASE_URL", "SENTINEL_DB_POOL_MIN", "SENTINEL_DB_POOL_MAX"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.database_url == ""
    assert (s.sentinel_db_pool_min, s.sentinel_db_pool_max) == (2, 10)


def test_db_pool_settings_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/sentinel")
    monkeypatch.setenv("SENTINEL_DB_POOL_MIN", "1")
    monkeypatch.setenv("SENTINEL_DB_POOL_MAX", "4")
    s = Settings()
    assert s.database_url == "postgresql://u:p@localhost:5432/sentinel"
    assert (s.sentinel_db_pool_min, s.sentinel_db_pool_max) == (1, 4)


@pytest.mark.parametrize(
    ("pool_min", "pool_max", "message"),
    [
        (-1, 10, "sentinel_db_pool_min must be >= 0"),
        (0, 0, "sentinel_db_pool_max must be >= 1"),
        (5, 2, "sentinel_db_pool_min must be <= sentinel_db_pool_max"),
    ],
)
def test_db_pool_invalid_bounds_raise(pool_min: int, pool_max: int, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(
            sentinel_db_pool_min=pool_min,
            sentinel_db_pool_max=pool_max,
        )


# ── The app boots with no secrets, no DATABASE_URL, no LangFuse keys ──────────


def test_app_lifespan_boots_without_any_secret(
    no_settings_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real lifespan (get_settings → SDK defaults → routers) starts and serves /health.

    The embedding model and the global trace-processor registry are stubbed; the
    subprocess test below boots the unstubbed CLI.
    """
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setattr(main, "EmbeddingClient", MagicMock())
    monkeypatch.setattr(main, "install_trace_processors", MagicMock())
    monkeypatch.setenv("SENTINEL_DEV_OPEN_DASHBOARD", "1")  # /api/config reachable without a token

    with TestClient(main.create_app()) as client:
        response = client.get("/health")
        config = client.get("/api/config")
        webhook = client.post(
            "/webhooks/incident",
            json={"signal_type": "runtime_error", "correlation_id": str(uuid4()), "title": "t"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    # No LLM key either: the §4.7 model view still serves (keys are read only at use).
    assert config.status_code == 200
    assert config.json()["roles"]["triage"]["primary"] == "anthropic/claude-haiku-4-5"
    # The dev flag opens the dashboard only: the webhook still needs a token.
    assert webhook.status_code == 401


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def serve_process(tmp_path: Path) -> Iterator[tuple[subprocess.Popen[bytes], int, Path]]:
    """`sentinel serve` in its own process group, from an empty dir, with no credentials.

    The environment drops every Settings variable (and LangFuse's), and
    ``LITELLM_MODE=PRODUCTION`` stops litellm from loading the repo's ``.env``, so
    nothing secret can reach the process.
    """
    env = {k: v for k, v in os.environ.items() if k.upper() not in _SETTINGS_ENV_VARS}
    env["LITELLM_MODE"] = "PRODUCTION"
    env["SENTINEL_DB_PATH"] = str(tmp_path / "sentinel.db")
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(_REPO_ROOT / "src"), env.get("PYTHONPATH", "")) if p
    )
    port = _free_port()
    log_path = tmp_path / "serve.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-c", "from sentinel.cli import main; main()", "serve"]
            + ["--port", str(port)],
            cwd=tmp_path,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        yield proc, port, log_path
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()


def test_sentinel_serve_boots_without_llm_keys_database_or_langfuse(
    serve_process: tuple[subprocess.Popen[bytes], int, Path],
) -> None:
    """`sentinel serve` with no LLM key, no DATABASE_URL and no LangFuse keys serves."""
    proc, port, log_path = serve_process
    deadline = time.monotonic() + 120
    status: int | None = None
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                status = resp.status
                break
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.5)

    log = log_path.read_text(errors="replace")
    assert status == 200, f"serve did not come up (exit={proc.poll()}):\n{log[-4000:]}"
    assert "sentinel_ready" in log

    # Every route but the probes needs a token; none is configured here.
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=5)
    assert err.value.code == 401
    log = log_path.read_text(errors="replace")
    assert "auth_not_configured" in log
