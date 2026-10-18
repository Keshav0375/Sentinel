"""Provider routing — the §4.7 per-role model map, credentials at use, and fallback.

Every LLM call in Sentinel runs on a fixed model chosen by its *role*. The map below is the
§4.7 table verbatim; ``Settings.sentinel_primary_provider`` picks which provider of each pair
runs first, and a provider error re-runs the agent once on the other one::

    primary, fallback = model_for("triage", settings)        # Model objects, keys read now
    routed = await call_with_fallback(agent, text, role="triage", settings=settings)
    routed.result.final_output, routed.model_used             # model_used: what actually ran

The judge is not a role here: it always runs on the provider the agents do NOT use
(decision 2026-10-15), so an agent never grades itself — see :func:`judge_model_string`.

Credentials are read when a model is built, never at boot (§8.2; decision 2026-10-14), and
only through :func:`provider_credential`. That function is the single seam backend phase 7
replaces with a workload-identity token exchange (decision 2026-08-30).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, get_args

import openai
from agents import Agent, Model, RunConfig, Runner, RunResult, TResponseInputItem
from agents.extensions.models.litellm_model import LitellmModel

from sentinel.config import Settings
from sentinel.exceptions import ProviderUnavailableError
from sentinel.infra.logging import get_logger
from sentinel.providers.capabilities import ProviderCapabilities, common_capabilities
from sentinel.providers.fake import FakeModel

__all__ = [
    "JUDGE_MODELS",
    "MODEL_MAP",
    "PROVIDERS",
    "ROLES",
    "Provider",
    "Role",
    "RoutedRun",
    "build_model",
    "call_with_fallback",
    "judge_model_string",
    "model_for",
    "model_name",
    "other_provider",
    "provider_credential",
    "provider_of",
    "role_capabilities",
    "role_models",
]

logger = get_logger("sentinel.providers.router")

Provider = Literal["anthropic", "openai"]
Role = Literal[
    "orchestrator",
    "triage",
    "analysis",
    "reflexion",
    "resolution",
    "reverification",
    "pr_content_generator",
]

PROVIDERS: Final[tuple[Provider, ...]] = get_args(Provider)
ROLES: Final[tuple[Role, ...]] = get_args(Role)

# §4.7 "Per-Agent Model Assignment": role → {provider: model}. The Anthropic column is the
# "Default (Anthropic)" model, the OpenAI column the "Fallback (OpenAI)" one; with
# SENTINEL_PRIMARY_PROVIDER=openai the two swap.
MODEL_MAP: Final[Mapping[Role, Mapping[Provider, str]]] = {
    "orchestrator": {"anthropic": "anthropic/claude-sonnet-4-6", "openai": "openai/gpt-4o"},
    "triage": {"anthropic": "anthropic/claude-haiku-4-5", "openai": "openai/gpt-4o-mini"},
    "analysis": {"anthropic": "anthropic/claude-sonnet-4-6", "openai": "openai/gpt-4o"},
    "reflexion": {"anthropic": "anthropic/claude-haiku-4-5", "openai": "openai/gpt-4o-mini"},
    "resolution": {"anthropic": "anthropic/claude-sonnet-4-6", "openai": "openai/gpt-4o"},
    "reverification": {
        "anthropic": "anthropic/claude-haiku-4-5",
        "openai": "openai/gpt-4o-mini",
    },
    "pr_content_generator": {
        "anthropic": "anthropic/claude-haiku-4-5",
        "openai": "openai/gpt-4o-mini",
    },
}

# §4.7 Judge row (decision 2026-10-15): primary provider → the judge model, always on the
# OTHER provider. The judge has no fallback — falling back would put it on the agents'
# provider.
JUDGE_MODELS: Final[Mapping[Provider, str]] = {
    "anthropic": "openai/gpt-4o",
    "openai": "anthropic/claude-sonnet-4-6",
}

# Settings field holding each provider's API key (until phase 7 federates the pod).
_CREDENTIAL_FIELDS: Final[Mapping[Provider, str]] = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
}

# A primary-provider failure that re-runs the agent on the fallback: the openai.APIError
# hierarchy (litellm's RateLimitError, Timeout, APIError and the other status errors all
# subclass it) and timeouts. asyncio.TimeoutError is an alias of the builtin since 3.11.
_FALLBACK_ERRORS: Final = (openai.APIError, TimeoutError)


def provider_of(model_string: str) -> Provider:
    """The provider of a ``provider/model`` string.

    Raises:
        ValueError: The prefix is not ``anthropic`` or ``openai`` (§4.7 allows no other).
    """
    prefix = model_string.split("/", 1)[0].lower() if "/" in model_string else ""
    for provider in PROVIDERS:
        if prefix == provider:
            return provider
    raise ValueError(
        f"{model_string!r} is not an 'anthropic/…' or 'openai/…' model string "
        f"(supported providers: {', '.join(PROVIDERS)})"
    )


def other_provider(provider: Provider) -> Provider:
    """The provider that is not *provider*."""
    return "openai" if provider == "anthropic" else "anthropic"


def role_models(role: Role, primary: Provider) -> tuple[str, str]:
    """``(primary_model, fallback_model)`` strings for *role* under *primary* (§4.7)."""
    models = MODEL_MAP[role]
    return models[primary], models[other_provider(primary)]


def judge_model_string(primary: Provider) -> str:
    """The judge's model: on the non-primary provider, and never an agent's model.

    Raises:
        RuntimeError: The judge model equals the model some agent role runs on under
            *primary* — that would be self-grading (decision 2026-10-15).
    """
    judge = JUDGE_MODELS[primary]
    agent_models = {role_models(role, primary)[0] for role in ROLES}
    if judge in agent_models:
        raise RuntimeError(
            f"judge model {judge!r} is also an agent model under primary={primary!r} "
            "(self-grading is forbidden, decision 2026-10-15)"
        )
    return judge


def _check_model_map() -> None:
    """Fail at import if the map breaks a §4.7 invariant: every role has a model on each
    provider, under the right prefix, and the judge differs from every agent model."""
    for role in ROLES:
        for provider in PROVIDERS:
            if provider_of(MODEL_MAP[role][provider]) != provider:
                raise RuntimeError(f"MODEL_MAP[{role!r}][{provider!r}] is on the wrong provider")
    for primary in PROVIDERS:
        if provider_of(judge_model_string(primary)) == primary:
            raise RuntimeError(f"the judge must not run on the primary provider {primary!r}")


_check_model_map()


def provider_credential(provider: Provider, settings: Settings) -> str | None:
    """The credential for *provider*, read now; None when it is not configured.

    Today: the API key from Settings. Backend phase 7 replaces this one function with a
    workload-identity token exchange (decision 2026-08-30); no caller reads a key itself.
    """
    secret = getattr(settings, _CREDENTIAL_FIELDS[provider])
    return None if secret is None else secret.get_secret_value()


def build_model(model_string: str, settings: Settings) -> Model:
    """A ready-to-run :class:`Model` for *model_string*, with its credential read now.

    With ``sentinel_fake_llm`` it is a :class:`FakeModel` (no key, no network).

    Raises:
        ProviderUnavailableError: The provider's credential is not configured.
    """
    provider = provider_of(model_string)
    if settings.sentinel_fake_llm:
        return FakeModel(model_string)
    credential = provider_credential(provider, settings)
    if credential is None:
        raise ProviderUnavailableError(
            f"{model_string!r} needs {_CREDENTIAL_FIELDS[provider].upper()}, which is not set"
        )
    return LitellmModel(model=model_string, api_key=credential)


def model_name(model: Model) -> str:
    """The ``provider/model`` string a :class:`Model` from :func:`model_for` runs."""
    name = getattr(model, "model", None)
    return name if isinstance(name, str) else type(model).__name__


def model_for(role: Role, settings: Settings) -> tuple[Model, Model | None]:
    """``(primary_model, fallback_model)`` for *role*, credentials read now.

    With ``sentinel_fake_llm`` both are :class:`FakeModel` and no key is read. A missing
    primary-provider credential makes the fallback the model to run (logged as
    ``primary_provider_failed``), with no second fallback.

    Raises:
        ProviderUnavailableError: Neither provider has a credential. The orchestrator
            turns this into an escalation.
    """
    primary_s, fallback_s = role_models(role, settings.sentinel_primary_provider)
    if settings.sentinel_fake_llm:
        return build_model(primary_s, settings), build_model(fallback_s, settings)
    primary_ok = provider_credential(provider_of(primary_s), settings) is not None
    fallback_ok = provider_credential(provider_of(fallback_s), settings) is not None

    if primary_ok:
        fallback = build_model(fallback_s, settings) if fallback_ok else None
        return build_model(primary_s, settings), fallback
    if fallback_ok:
        logger.warning(
            "primary_provider_failed",
            provider=provider_of(primary_s),
            falling_back_to=fallback_s,
            role=role,
            error_type="MissingCredential",
        )
        return build_model(fallback_s, settings), None
    raise ProviderUnavailableError(
        f"no LLM provider is configured for role {role!r}: set ANTHROPIC_API_KEY or OPENAI_API_KEY"
    )


def role_capabilities(role: Role, settings: Settings) -> ProviderCapabilities:
    """What an agent for *role* may rely on: features both models of its pair support."""
    return common_capabilities(*role_models(role, settings.sentinel_primary_provider))


@dataclass(frozen=True)
class RoutedRun:
    """A finished :func:`call_with_fallback` run and the model that produced it."""

    result: RunResult
    model_used: str
    fell_back: bool


async def call_with_fallback(
    agent: Agent[Any],
    input: str | list[TResponseInputItem],
    *,
    role: Role,
    settings: Settings,
    run_kwargs: Mapping[str, Any] | None = None,
) -> RoutedRun:
    """Run *agent* on its role's primary model; on a provider error, once on the fallback.

    The §4.7 fallback: ``Runner.run(agent.clone(model=primary))``; on the openai.APIError
    hierarchy or a timeout, log ``primary_provider_failed`` and re-run with
    ``agent.clone(model=fallback)``. Any other error, or a failure with no fallback
    available, propagates.

    Args:
        agent: The agent to run; its own ``model`` is replaced by the routed one.
        input: The run input, as for ``Runner.run``.
        role: The §4.7 role whose models to use.
        settings: Read for the primary provider and the credentials.
        run_kwargs: Extra ``Runner.run`` keyword arguments (``max_turns``, ``context``…),
            passed to both attempts. Their ``run_config`` (or a default one) always gets
            ``trace_include_sensitive_data=False``.

    Returns:
        The run result and the ``provider/model`` string that actually produced it.

    Raises:
        ProviderUnavailableError: Neither provider has a credential.
    """
    kwargs = dict(run_kwargs or {})
    # Spans never carry model or tool inputs/outputs (decision 2026-10-15).
    base_config = kwargs.get("run_config")
    kwargs["run_config"] = replace(
        base_config if isinstance(base_config, RunConfig) else RunConfig(),
        trace_include_sensitive_data=False,
    )
    primary, fallback = model_for(role, settings)
    try:
        result = await Runner.run(agent.clone(model=primary), input, **kwargs)
        return RoutedRun(result=result, model_used=model_name(primary), fell_back=False)
    except _FALLBACK_ERRORS as exc:
        if fallback is None:
            raise
        logger.warning(
            "primary_provider_failed",
            provider=provider_of(model_name(primary)),
            falling_back_to=model_name(fallback),
            role=role,
            error_type=type(exc).__name__,
        )
    result = await Runner.run(agent.clone(model=fallback), input, **kwargs)
    return RoutedRun(result=result, model_used=model_name(fallback), fell_back=True)
