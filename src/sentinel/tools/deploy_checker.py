"""get_deploy_details tool — what deployed to a service, from ``deployments`` (backend.md §4.8).

The deploy↔incident correlation source (decision 2026-10-14, "Backend phase 3 (tools)
contract"): the rows the deploy pipeline's record stage writes (ci_app_deployment.yml,
one per attempt), newest first, ``failed`` attempts included — they are the correlation
targets.

Read-only: a SELECT on ``deployments`` (and on ``services`` for the repo when enriching).
The ``deployments.incident_id`` backfill is not a tool action.

GitHub enrichment is optional. Only with ``settings.github_api_token`` set, the newest
few rows with a PR number get ``GET /repos/{repo}/pulls/{n}``, where ``repo`` is the
service's ``services.metadata.repo`` — never a model argument. A lookup that fails leaves
``github`` None with a ``github_note`` and never fails the call. The token is read only
when the request headers are built and is never logged.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any, cast

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
import httpx
from agents import FunctionTool, function_tool

from sentinel.config import Settings, secret_value
from sentinel.exceptions import SentinelMemoryError
from sentinel.infra import database
from sentinel.infra.logging import get_logger
from sentinel.models.tools import (
    DeployQuery,
    DeployRecord,
    GitHubPR,
    ToolFailure,
    ToolFailureKind,
)
from sentinel.tools.output import tool_json

logger = get_logger("sentinel.tools.deploy_checker")

_TOOL_NAME = "get_deploy_details"

# ── Bounds ────────────────────────────────────────────────────────────────────
_MIN_WINDOW_HOURS = 1
_MAX_WINDOW_HOURS = 168
_MIN_LIMIT = 1
_MAX_LIMIT = 50

# ── GitHub enrichment ─────────────────────────────────────────────────────────
_MAX_ENRICHED = 5  # newest rows with a PR number that are looked up per call
_GITHUB_API = "https://api.github.com"
_GITHUB_API_VERSION = "2022-11-28"
_GITHUB_TIMEOUT = httpx.Timeout(5.0)
_TITLE_CHARS = 200

# ── Payload caps (the result goes to the model, so it stays small) ───────────
_AUTHOR_CHARS = 100
_MAX_FILES = 20  # files_changed entries per record; files_changed_total has the count

# The service is a bind parameter, but a non-name is refused before it reaches the DB.
_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
# ``owner/name`` as GitHub allows them; the repo is spliced into the request path.
_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")

_DEPLOYS_SQL = """
    SELECT pr_number, commit_sha, author, deploy_status, deployed_at, files_changed,
           metadata->>'failed_stage' AS failed_stage,
           metadata->>'version' AS version,
           gha_run_id
    FROM deployments
    WHERE service = $1 AND deployed_at >= now() - make_interval(hours => $2)
    ORDER BY deployed_at DESC
    LIMIT $3
"""

_REPO_SQL = "SELECT metadata->>'repo' FROM services WHERE name = $1"


def make_deploy_checker_tool(
    pool: asyncpg.Pool | None,
    settings: Settings,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> FunctionTool:
    """Create the get_deploy_details tool with the pool and settings injected.

    Args:
        pool: asyncpg pool holding ``deployments``; None (no ``DATABASE_URL``) makes
            every call return a ToolFailure ("unavailable"), never an empty list.
        settings: Supplies ``github_api_token``, read at call time; unset → no
            GitHub enrichment.
        http_client: Client for the GitHub requests (tests pass one over a mock
            transport). None → a short-lived client per call.

    Returns:
        FunctionTool that agents can call to list a service's recent deploys.
    """

    @function_tool(strict_mode=False)
    async def get_deploy_details(request: DeployQuery) -> str:
        """List what deployed to a service in the last window_hours, newest first.

        One entry per deploy attempt the CI pipeline recorded, failed ones included:
        deploy_status (succeeded | failed | rolled_back), failed_stage (build, deploy,
        verify, or "none"), PR number, commit SHA, author, deploy time (UTC), files
        changed (the first 20, with files_changed_total), version ("pr-<n>-<sha>") and
        the GitHub Actions run id. When GitHub is
        configured the newest few deploys also carry their PR (title, link, merge time,
        author) in ``github``; otherwise ``github`` is null and ``github_note`` says
        why. Returns a JSON array; an empty array means nothing was deployed in the
        window. A failure ("error": true, kind "unavailable") means the deploy history
        could not be read — no finding either way.

        Args:
            request: The service, the window in hours, and how many deploys to return.
        """
        return tool_json(
            await _get_deploy_details(pool, settings, request, http_client=http_client)
        )

    return get_deploy_details


async def _get_deploy_details(
    pool: asyncpg.Pool | None,
    settings: Settings,
    request: DeployQuery,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> list[DeployRecord] | ToolFailure:
    """Read the service's recent deploys, or return a ToolFailure — never raise.

    Separated from the tool decorator so tests can call it without a ToolContext.
    """
    service = request.service.strip()
    if not _SERVICE_RE.fullmatch(service):
        return _failure("bad_input", f"service {service!r} is not a service name")
    hours = request.window_hours
    if not _MIN_WINDOW_HOURS <= hours <= _MAX_WINDOW_HOURS:
        return _failure(
            "bad_input",
            f"window_hours must be {_MIN_WINDOW_HOURS}-{_MAX_WINDOW_HOURS}, got {hours}",
        )
    limit = request.limit
    if not _MIN_LIMIT <= limit <= _MAX_LIMIT:
        return _failure("bad_input", f"limit must be {_MIN_LIMIT}-{_MAX_LIMIT}, got {limit}")

    if pool is None:
        # An empty list here would read as "nothing deployed" — a different, false finding.
        return _failure("unavailable", "deploy history cannot be read: no database is configured")
    enrich = settings.github_api_token is not None
    try:
        async with database.acquire(pool) as conn:
            rows: list[asyncpg.Record] = await conn.fetch(_DEPLOYS_SQL, service, hours, limit)
            repo: str | None = None
            if enrich and any(_pr_number(row["pr_number"]) for row in rows):
                repo = await conn.fetchval(_REPO_SQL, service)
    except SentinelMemoryError as exc:
        logger.warning("deploy_details_db_unavailable", service=service, error=str(exc))
        return _failure("unavailable", f"deploy history cannot be read: {exc}")

    records = [_record(row) for row in rows]
    if enrich:
        records = await _enrich(records, settings, service, repo, http_client)
    else:
        records = _noted(records, range(len(records)), "not looked up: GITHUB_API_TOKEN not set")

    logger.info(
        "deploy_details_done",
        service=service,
        window_hours=hours,
        deploys=len(records),
        enriched=sum(record.github is not None for record in records),
    )
    return records


def _failure(kind: ToolFailureKind, message: str) -> ToolFailure:
    return ToolFailure(tool=_TOOL_NAME, kind=kind, message=message)


# ── Rows ──────────────────────────────────────────────────────────────────────


def _record(row: asyncpg.Record) -> DeployRecord:
    """A ``deployments`` row → DeployRecord (failed_stage/version come from ``metadata``)."""
    files = _files(row["files_changed"])
    return DeployRecord(
        pr_number=_pr_number(row["pr_number"]),
        commit_sha=row["commit_sha"],
        author=_author(row["author"]),
        deploy_status=row["deploy_status"],
        deployed_at=row["deployed_at"],
        files_changed=files[:_MAX_FILES],
        files_changed_total=len(files),
        failed_stage=row["failed_stage"],
        version=row["version"],
        gha_run_id=row["gha_run_id"],
    )


def _author(value: object) -> str | None:
    """An author name or login within ``_AUTHOR_CHARS``."""
    return value[:_AUTHOR_CHARS] if isinstance(value, str) else None


def _pr_number(value: object) -> int | None:
    """The PR number; the pipeline records 0 for a direct push (no PR)."""
    return value if isinstance(value, int) and value > 0 else None


def _files(raw: object) -> list[str]:
    """``files_changed`` JSONB (asyncpg returns it as text) → its string items."""
    if not isinstance(raw, str):
        return []
    try:
        data: object = json.loads(raw)
    except ValueError:
        return []
    items = cast(list[object], data) if isinstance(data, list) else []
    return [item for item in items if isinstance(item, str)]


def _noted(records: list[DeployRecord], indexes: Iterable[int], note: str) -> list[DeployRecord]:
    """*records* with *note* on each listed record that has a PR number."""
    chosen = set(indexes)
    return [
        record.model_copy(update={"github_note": note})
        if i in chosen and record.pr_number is not None
        else record
        for i, record in enumerate(records)
    ]


# ── GitHub ────────────────────────────────────────────────────────────────────


async def _enrich(
    records: list[DeployRecord],
    settings: Settings,
    service: str,
    repo: str | None,
    http_client: httpx.AsyncClient | None,
) -> list[DeployRecord]:
    """Attach the PR to the newest ``_MAX_ENRICHED`` records with a PR number.

    Never raises for a GitHub problem: a failed lookup becomes that record's note.
    """
    with_pr = [i for i, record in enumerate(records) if record.pr_number is not None]
    if not with_pr:
        return records
    if repo is None or not _REPO_RE.fullmatch(repo):
        return _noted(records, with_pr, f"not looked up: no GitHub repo recorded for {service}")

    looked_up, skipped = with_pr[:_MAX_ENRICHED], with_pr[_MAX_ENRICHED:]
    records = _noted(
        records, skipped, f"not looked up: only the newest {_MAX_ENRICHED} PRs are fetched"
    )
    numbers = list(dict.fromkeys(cast(int, records[i].pr_number) for i in looked_up))
    headers = _github_headers(settings)
    if http_client is None:
        async with httpx.AsyncClient(timeout=_GITHUB_TIMEOUT) as client:
            found = await _fetch_prs(client, repo, numbers, headers)
    else:
        found = await _fetch_prs(http_client, repo, numbers, headers)

    enriched = list(records)
    for i in looked_up:
        result = found[cast(int, records[i].pr_number)]
        update = {"github": result} if isinstance(result, GitHubPR) else {"github_note": result}
        enriched[i] = records[i].model_copy(update=update)
    return enriched


def _github_headers(settings: Settings) -> dict[str, str]:
    """The GitHub request headers — the only place the token value is read."""
    token = secret_value(settings.github_api_token, "GITHUB_API_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
    }


async def _fetch_prs(
    client: httpx.AsyncClient, repo: str, numbers: list[int], headers: dict[str, str]
) -> dict[int, GitHubPR | str]:
    """Each PR by number, or the note saying why it could not be read."""
    results = await asyncio.gather(*(_fetch_pr(client, repo, n, headers) for n in numbers))
    return dict(zip(numbers, results, strict=True))


async def _fetch_pr(
    client: httpx.AsyncClient, repo: str, number: int, headers: dict[str, str]
) -> GitHubPR | str:
    """GET one PR; a GitHub problem becomes a note naming PR and reason, never the token."""
    what = f"GitHub lookup of PR #{number} failed"
    try:
        response = await client.get(
            f"{_GITHUB_API}/repos/{repo}/pulls/{number}",
            headers=headers,
            timeout=_GITHUB_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return _lookup_failed(number, f"{what}: {type(exc).__name__}")
    if not response.is_success:
        return _lookup_failed(number, f"{what}: HTTP {response.status_code}")
    try:
        data: object = response.json()
    except ValueError:
        return _lookup_failed(number, f"{what}: non-JSON body")
    pr = _github_pr(data)
    return pr if pr is not None else _lookup_failed(number, f"{what}: unexpected body")


def _lookup_failed(number: int, note: str) -> str:
    logger.warning("github_pr_lookup_failed", pr_number=number, reason=note)
    return note


def _github_pr(data: object) -> GitHubPR | None:
    """A pulls API body → GitHubPR (title, html_url, merged_at, user.login)."""
    if not isinstance(data, dict):
        return None
    body = cast(dict[str, Any], data)
    title, html_url = body.get("title"), body.get("html_url")
    if not isinstance(title, str) or not isinstance(html_url, str):
        return None
    user: object = body.get("user")
    login = cast(dict[str, Any], user).get("login") if isinstance(user, dict) else None
    return GitHubPR(
        title=title[:_TITLE_CHARS],
        html_url=html_url,
        merged_at=_parse_time(body.get("merged_at")),
        author=_author(login),
    )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
