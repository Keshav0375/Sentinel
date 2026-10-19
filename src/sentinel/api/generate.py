"""POST /generate/pr-content — the revert PR's title and description (backend.md §3.4).

One consumer: the ``generate-pr-content`` job of ci_incident_response.yml, right before
``create-rollback-pr``. Text only: the endpoint never opens a PR, calls GitHub or writes
the database. The service comes from the stored incident, never from the body, and the
target is cross-checked against ``deployments`` (decision 2026-10-14) and pinned to the
verified row before the text is written.

Failures: 404 the incident does not exist; 409 no matching ``deployments`` row or the
row disagrees with the target; 503 the incident or the target cannot be checked (no
database, database error) or no LLM provider is configured; 502 the provider failed
after its fallback. Authentication is applied by the caller at ``include_router``.
"""

from __future__ import annotations

from typing import Final

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
import openai
from fastapi import APIRouter, HTTPException

from sentinel.agents.pr_content_generator import generate_pr_content
from sentinel.config import Settings
from sentinel.exceptions import ProviderUnavailableError, SentinelMemoryError
from sentinel.infra.logging import get_logger
from sentinel.memory.episodic import EpisodicMemory
from sentinel.models.pr_content import PRContent
from sentinel.models.tools import DeployRecord, RollbackSpec, TargetDeploy
from sentinel.tools.deploy_checker import check_rollback_target

__all__ = ["GENERATE_PATH", "make_generate_router"]

logger = get_logger("sentinel.api.generate")

GENERATE_PATH: Final = "/generate/pr-content"

_CHECK_UNAVAILABLE: Final = "the rollback target cannot be checked: the database is unavailable"


def make_generate_router(
    *,
    episodic_memory: EpisodicMemory,
    pool: asyncpg.Pool | None,
    settings: Settings,
) -> APIRouter:
    """Create the PR-content router.

    Args:
        episodic_memory: Where the incident (and so its service) is looked up.
        pool: The database pool the rollback target is cross-checked on; None when
            no database is configured (the endpoint then answers 503).
        settings: Application settings (LLM provider keys, fake-LLM switch).

    Returns:
        ``APIRouter`` with ``POST /generate/pr-content`` under no prefix and no
        dependencies: the caller applies authentication at ``include_router``.
    """
    router = APIRouter()

    @router.post(GENERATE_PATH, response_model=PRContent)
    async def post_pr_content(spec: RollbackSpec) -> PRContent:  # pyright: ignore[reportUnusedFunction]
        """Write the revert PR text for a rollback spec whose target is a recorded deploy."""
        if pool is None:
            logger.warning("pr_content_no_database", incident_id=spec.incident_id)
            raise HTTPException(
                status_code=503,
                detail="no database is configured: the incident and its rollback target "
                "cannot be checked",
            )
        try:
            incident = await episodic_memory.get_incident(spec.incident_id)
        except SentinelMemoryError as exc:
            logger.error(
                "pr_content_incident_unavailable", incident_id=spec.incident_id, error=str(exc)
            )
            raise HTTPException(
                status_code=503, detail="the incident store is unavailable"
            ) from exc
        if incident is None:
            raise HTTPException(status_code=404, detail=f"incident {spec.incident_id!r} not found")

        try:
            check = await check_rollback_target(pool, incident.service_name, spec.target_deploy)
        except SentinelMemoryError as exc:  # defensive: documented never to raise
            logger.error(
                "pr_content_target_check_unavailable",
                incident_id=spec.incident_id,
                error=str(exc),
            )
            raise HTTPException(status_code=503, detail=_CHECK_UNAVAILABLE) from exc
        if check.unavailable:
            logger.error(
                "pr_content_target_check_unavailable",
                incident_id=spec.incident_id,
                error=check.problem,
            )
            raise HTTPException(status_code=503, detail=_CHECK_UNAVAILABLE)
        if not check.ok or check.record is None:
            problem = check.problem or "the rollback target matched no deployments row"
            logger.warning(
                "pr_content_target_rejected",
                incident_id=spec.incident_id,
                service=incident.service_name,
                problem=problem,
            )
            raise HTTPException(status_code=409, detail=problem)

        pinned = _pinned(spec, check.record)
        try:
            content = await generate_pr_content(pinned, settings=settings)
        except ProviderUnavailableError as exc:
            logger.error(
                "pr_content_provider_unavailable", incident_id=spec.incident_id, error=str(exc)
            )
            raise HTTPException(status_code=503, detail="no LLM provider is configured") from exc
        except (openai.APIError, TimeoutError) as exc:
            logger.error(
                "pr_content_provider_failed",
                incident_id=spec.incident_id,
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=502, detail="the LLM provider failed to generate the PR content"
            ) from exc

        logger.info(
            "pr_content_generated",
            incident_id=spec.incident_id,
            deploy_id=check.deploy_id,
            model_used=content.model_used,
            tokens_used=content.tokens_used,
        )
        return content

    return router


def _pinned(spec: RollbackSpec, record: DeployRecord) -> RollbackSpec:
    """*spec* with its target set to the verified deployments row.

    The row's full commit SHA, PR number and deploy time replace the body's.
    """
    pr_number = record.pr_number if record.pr_number is not None else spec.target_deploy.pr_number
    target = TargetDeploy(
        pr_number=pr_number,
        commit_sha=record.commit_sha,
        deployed_at=record.deployed_at,
    )
    return spec.model_copy(update={"target_deploy": target})
