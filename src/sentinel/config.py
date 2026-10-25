"""Environment-variable-driven settings via pydantic-settings."""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# §4.6: at most 20 tool calls per incident — a hard cap, not a default to raise.
MAX_TOOL_CALLS_CAP = 20

# One agent's whole Runner.run (providers/router.py call_with_fallback). One model call's
# worst case is 3 attempts x 60s + the 1s and 2s waits = 183s; a run makes several model
# calls (a tool-call turn, then the answer; up to the budget's turns). 600s fits three
# worst-case calls or a long multi-turn tool run. The bounds keep the setting above one
# call's worst case (router.py checks it at import) and below a runaway.
AGENT_RUN_TIMEOUT_S = 600.0
AGENT_RUN_TIMEOUT_MIN_S = 240.0
AGENT_RUN_TIMEOUT_MAX_S = 1800.0

# POST /webhooks/incident body cap in bytes; over it the request is 413 before parsing or auth.
WEBHOOK_MAX_BODY_BYTES = 256 * 1024
# One incident's whole plan-execute loop, in seconds; past it the incident escalates (and is
# still stored). A run is several agent calls of up to AGENT_RUN_TIMEOUT_S each.
PIPELINE_TIMEOUT_S = 900.0
# In-flight incident pipelines accepted at once; beyond it POST /webhooks/incident is 429.
PIPELINE_MAX_CONCURRENT = 5
# SIGTERM: how long shutdown waits for in-flight incidents before logging and cancelling them.
SHUTDOWN_GRACE_S = 20.0

# Every credential field. All optional: no secret is required to boot, and a feature whose
# key is missing fails at use (backend.md §8.2; decision 2026-10-14). A blank value counts
# as missing.
_SECRET_FIELDS = (
    "openai_api_key",
    "anthropic_api_key",
    "dd_api_key",
    "dd_app_key",
    "github_api_token",
    "langfuse_public_key",
    "langfuse_secret_key",
)


class Settings(BaseSettings):
    """All runtime configuration loaded from environment variables / .env file.

    Intended to be instantiated once at startup and injected via FastAPI Depends
    or passed explicitly — never imported as a module-level singleton.

    No field is required: the app boots with no credentials at all. Secrets are
    ``SecretStr | None`` (masked in repr and logs); read one with ``secret_value``,
    which raises a clear error naming the env var when it is unset.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM providers (§4.7) ──────────────────────────────────────────────────
    # Anthropic and OpenAI only. Each agent role has a fixed model pair (§4.7 table, in
    # providers/router.py); this flag picks which side of the pair runs first. The judge
    # always runs on the other provider. Models are not configurable per environment.
    sentinel_primary_provider: Literal["anthropic", "openai"] = "anthropic"

    # Read at use by providers/router.py, never at boot. A missing primary-provider key
    # falls back to the other provider; with neither, the incident escalates.
    # In the pod they come from Key Vault (anthropic-api-key, openai-api-key; decision
    # 2026-10-24), never from env vars.
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    # Local sentence-transformers model for zero-cost embedding (384 dims).
    sentinel_embedding_model: str = "all-MiniLM-L6-v2"

    # ── PostgreSQL + pgvector (Phase 2) ───────────────────────────────────────
    # asyncpg DSN (postgresql://…). Empty → no Postgres pool is created; the app still
    # boots and every memory call reports "memory unavailable" (decision 2026-10-12).
    database_url: str = ""

    # asyncpg pool bounds — one connection per concurrent incident (backend.md §4.5).
    sentinel_db_pool_min: int = 2
    sentinel_db_pool_max: int = 10

    sentinel_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Hard cap on tool calls per incident to prevent runaway loops (§4.6): 20 is the
    # ceiling, so the setting may lower the cap but never raise it.
    sentinel_max_tool_calls: int = MAX_TOOL_CALLS_CAP

    # The bound on one agent's Runner.run, in seconds (see AGENT_RUN_TIMEOUT_S above); a
    # primary run past it falls back to the other provider.
    sentinel_agent_run_timeout_s: float = AGENT_RUN_TIMEOUT_S

    # Every role runs on the deterministic, no-network FakeModel (providers/fake.py) in
    # tests/CI (§9.1); no LLM key is read. /ready skips the live-provider check.
    sentinel_fake_llm: bool = False

    # ── LangFuse (§6) ─────────────────────────────────────────────────────────
    # The langfuse v3 client reads LANGFUSE_* from the process environment itself
    # (infra/tracing.configure_langfuse); these fields mirror the same env vars.
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str | None = None

    # ── Datadog (fetch_logs — §4.8) ───────────────────────────────────────────
    # The app key is scoped to logs_read_data + synthetics_read (read-only).
    dd_api_key: SecretStr | None = None
    dd_app_key: SecretStr | None = None
    dd_site: str = "us5.datadoghq.com"

    # ── GitHub (get_deploy_details — §4.8) ────────────────────────────────────
    # Read-only fine-grained PAT on Keshav0375/Sentinel-deployment ("Pull requests: read" only).
    github_api_token: SecretStr | None = None

    # ── Inbound auth: Entra bearer (§3.6; decision 2026-10-19) ───────────────
    # Public identifiers, not secrets (ConfigMap / GitHub variables). Both unset → every
    # protected route returns 401 and startup logs auth_not_configured; boot still works.
    # The IDENTITY tenant (R4) holding the backend app registration — NOT AZURE_TENANT_ID,
    # which stays the workload-identity/resource tenant read by azure-identity.
    sentinel_api_tenant_id: str | None = None
    # Comma-separated accepted audiences: the API app registration's client-id GUID (required:
    # Entra v2 access tokens carry aud = the client ID) and optionally its identifier URI
    # api://<identity_tenant_id>/sentinel-backend-<deployment>-<env>.
    # Required config, no default (phase 7 sets it from infra's app-registration outputs).
    sentinel_api_audience: str | None = None

    # ── Azure runtime access (§8.2; decision 2026-10-19) ─────────────────────
    # Key Vault with datadog-api-key, datadog-app-key, langfuse-public-key/-secret-key/-base-url,
    # anthropic-api-key and openai-api-key (decision 2026-10-24), read once at startup (restart
    # to rotate; infra/workload_identity). Unset → env only. The GitHub PAT never comes from it.
    key_vault_url: str | None = None
    # Entra-only Azure PostgreSQL, used only when DATABASE_URL is blank: all three or none. The
    # password is an Entra token fetched per new connection
    # (infra/database.init_pool_from_settings).
    pghost: str | None = None
    pgdatabase: str | None = None
    pguser: str | None = None
    # Local only, never in the pod: serve GET /, /eval and the dashboard's read APIs without a
    # token.
    # The webhook, incidents and generate routes stay protected.
    sentinel_dev_open_dashboard: bool = False

    # ── Incident concurrency and shutdown (backend 5 review) ──────────────────
    sentinel_webhook_max_body_bytes: int = WEBHOOK_MAX_BODY_BYTES
    sentinel_max_concurrent_pipelines: int = PIPELINE_MAX_CONCURRENT
    # The whole plan-execute loop of one incident; past it the incident escalates (and is stored).
    sentinel_pipeline_timeout_s: float = PIPELINE_TIMEOUT_S
    # The grace sits inside K8s' default 30 s terminationGracePeriodSeconds: after the drain,
    # teardown needs up to ~2 s of cancel unwind plus the trace flush and close_pool (<= 10 s).
    # Raise it only together with the pod's terminationGracePeriodSeconds (deployment manifest).
    sentinel_shutdown_grace_s: float = SHUTDOWN_GRACE_S

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("sentinel_max_tool_calls")
    @classmethod
    def max_tool_calls_in_range(cls, v: int) -> int:
        if v < 1:
            raise ValueError("sentinel_max_tool_calls must be >= 1")
        if v > MAX_TOOL_CALLS_CAP:
            raise ValueError(
                f"sentinel_max_tool_calls must be <= {MAX_TOOL_CALLS_CAP} (the §4.6 hard cap)"
            )
        return v

    @field_validator("sentinel_agent_run_timeout_s")
    @classmethod
    def agent_run_timeout_in_range(cls, v: float) -> float:
        if not AGENT_RUN_TIMEOUT_MIN_S <= v <= AGENT_RUN_TIMEOUT_MAX_S:
            raise ValueError(
                f"sentinel_agent_run_timeout_s must be between {AGENT_RUN_TIMEOUT_MIN_S:g} "
                f"and {AGENT_RUN_TIMEOUT_MAX_S:g} seconds (above one model call's worst case)"
            )
        return v

    @field_validator("sentinel_webhook_max_body_bytes")
    @classmethod
    def webhook_max_body_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("sentinel_webhook_max_body_bytes must be >= 1")
        return v

    @field_validator("sentinel_max_concurrent_pipelines")
    @classmethod
    def max_concurrent_pipelines_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("sentinel_max_concurrent_pipelines must be >= 1")
        return v

    @field_validator("sentinel_pipeline_timeout_s")
    @classmethod
    def pipeline_timeout_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("sentinel_pipeline_timeout_s must be > 0")
        return v

    @field_validator("sentinel_shutdown_grace_s")
    @classmethod
    def shutdown_grace_not_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("sentinel_shutdown_grace_s must be >= 0")
        return v

    @model_validator(mode="after")
    def db_pool_bounds_valid(self) -> Settings:
        if self.sentinel_db_pool_min < 0:
            raise ValueError("sentinel_db_pool_min must be >= 0")
        if self.sentinel_db_pool_max < 1:
            raise ValueError("sentinel_db_pool_max must be >= 1")
        if self.sentinel_db_pool_min > self.sentinel_db_pool_max:
            raise ValueError("sentinel_db_pool_min must be <= sentinel_db_pool_max")
        return self

    @field_validator(
        *_SECRET_FIELDS,
        "langfuse_base_url",
        "sentinel_api_tenant_id",
        "sentinel_api_audience",
        "key_vault_url",
        "pghost",
        "pgdatabase",
        "pguser",
        mode="before",
    )
    @classmethod
    def blank_is_unset(cls, v: object) -> object:
        """A blank or whitespace-only value counts as missing (None)."""
        if isinstance(v, str) and not v.strip():
            return None
        return v


def get_settings() -> Settings:
    """Create a Settings instance from the current environment.

    Call this once at app startup and pass the result via dependency injection.
    In tests, instantiate Settings directly with explicit kwargs to avoid
    requiring a .env file.
    """
    return Settings()


def secret_value(secret: SecretStr | None, env_var: str) -> str:
    """Return *secret*'s plain value at the point of use.

    Raises:
        ValueError: The secret is unset; the message names *env_var* so the operator
            knows which key to provide. Callers turn this into their own failure.
    """
    if secret is None:
        raise ValueError(f"{env_var} is not set")
    return secret.get_secret_value()
