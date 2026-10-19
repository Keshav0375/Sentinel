"""Tests for sentinel.tools.deploy_checker — the get_deploy_details tool.

Tests taking ``pool`` run against a real pgvector container (TEST_DATABASE_URL, see
tests/conftest.py): this module migrates one throwaway database to head — so the 002
seed row ``sentinel-watchtower`` (metadata repo ``Keshav0375/Sentinel-deployment``)
exists — empties ``deployments`` before each test and drops the database afterwards.
They skip with a reason when TEST_DATABASE_URL is not set (fail in CI).

GitHub is never called: the enrichment runs over ``httpx.MockTransport``, which records
each request so the tests can check method, URL and headers.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
from agents import FunctionTool
from agents.tool_context import ToolContext
from alembic import command
from alembic.config import Config

from sentinel.config import Settings
from sentinel.infra.database import create_pool
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.models.tools import DeployQuery, DeployRecord, GitHubPR, TargetDeploy, ToolFailure
from sentinel.tools import deploy_checker
from sentinel.tools.deploy_checker import (
    _get_deploy_details,
    check_rollback_target,
    make_deploy_checker_tool,
)

SERVICE = "sentinel-watchtower"  # the 002 seed row: metadata.repo = Keshav0375/Sentinel-deployment
REPO = "Keshav0375/Sentinel-deployment"
TOKEN = "github_pat_test-token-5f1e9c"

# ci_app_deployment.yml stage 5 ("Record deployment in PostgreSQL"), verbatim: the row
# shape the deploy pipeline really writes. psql interpolates each :'var' as a quoted
# literal on the client; _psql_interpolate does the same.
_PIPELINE_INSERT = """
INSERT INTO deployments
  (service, pr_number, commit_sha, author, deploy_status, gha_run_id, files_changed, metadata)
VALUES
  (:'service', :'pr'::int, :'sha', :'author', :'status', :'run'::bigint, :'files'::jsonb,
   jsonb_build_object('failed_stage', :'stage', 'version', :'version'));
"""

_WORKFLOW = (
    Path(__file__).resolve().parents[3]
    / "Sentinel-deployment"
    / ".github"
    / "workflows"
    / "ci_app_deployment.yml"
)


# ── Database fixtures ─────────────────────────────────────────────────────────


def _with_database(dsn: str, name: str) -> str:
    return urlunsplit(urlsplit(dsn)._replace(path=f"/{name}"))


def _execute(dsn: str, sql: str) -> None:
    async def _go() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(_go())


@pytest.fixture(scope="module")
def _module_db() -> Iterator[dict[str, str]]:
    """Holder for this module's throwaway database; dropped when the module finishes."""
    holder: dict[str, str] = {}
    yield holder
    if holder:
        _execute(holder["server"], f'DROP DATABASE IF EXISTS "{holder["name"]}" WITH (FORCE)')


@pytest.fixture
def deploy_dsn(
    pg_dsn: str,
    _module_db: dict[str, str],
    alembic_config: Callable[..., Config],
    seed_embedding_client: EmbeddingClient,
) -> str:
    """A database at head (the §5.3 schema + the 002 seed row) with no deployments."""
    if not _module_db:
        name = f"sentinel_deploys_{uuid.uuid4().hex[:12]}"
        _execute(pg_dsn, f'CREATE DATABASE "{name}"')
        _module_db.update(server=pg_dsn, name=name, dsn=_with_database(pg_dsn, name))
        command.upgrade(alembic_config(_module_db["dsn"], seed_embedding_client), "head")
    _execute(_module_db["dsn"], "DELETE FROM deployments")
    return _module_db["dsn"]


@pytest.fixture
async def pool(deploy_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(deploy_dsn, min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


async def _insert(
    pool: asyncpg.Pool,
    *,
    ago: timedelta,
    service: str = SERVICE,
    pr: int = 42,
    sha: str = "a1b2c3d",
    author: str | None = "keshav",
    status: str = "succeeded",
    run: int | None = 1001,
    files: list[str] | None = None,
    stage: str = "none",
    version: str | None = None,
) -> None:
    """One deployments row recorded *ago* before now."""
    await pool.execute(
        """
        INSERT INTO deployments (service, pr_number, commit_sha, author, deploy_status,
                                 gha_run_id, files_changed, deployed_at, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, now() - $8::interval,
                jsonb_build_object('failed_stage', $9::text, 'version', $10::text))
        """,
        service,
        pr,
        sha,
        author,
        status,
        run,
        json.dumps(files if files is not None else ["app/main.py"]),
        ago,
        stage,
        version or f"pr-{pr}-{sha}",
    )


def _quote_literal(value: str) -> str:
    """A SQL string literal, the way psql's :'var' writes one (E'' form for backslashes)."""
    escaped = value.replace("'", "''")
    if "\\" in value:
        return "E'" + escaped.replace("\\", "\\\\") + "'"
    return f"'{escaped}'"


def _psql_interpolate(sql: str, variables: dict[str, str]) -> str:
    return re.sub(r":'(\w+)'", lambda m: _quote_literal(variables[m.group(1)]), sql)


# ── GitHub fake ───────────────────────────────────────────────────────────────

Fault = Callable[[httpx.Request], httpx.Response]


def _pr_body(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Change #{number}",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "merged_at": "2026-10-14T09:30:00Z",
        "user": {"login": f"dev-{number}"},
        "body": "not passed on",
    }


class FakeGitHub:
    """Serves ``GET /repos/{repo}/pulls/{n}``; ``faults`` replaces the answer for a PR."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.faults: dict[int, Fault] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        number = int(request.url.path.rsplit("/", 1)[1])
        if number in self.faults:
            return self.faults[number](request)
        return httpx.Response(200, json=_pr_body(number), request=request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def numbers(self) -> list[int]:
        return [int(r.url.path.rsplit("/", 1)[1]) for r in self.requests]


class RecordingLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _record(self, event: str, **fields: Any) -> None:
        self.lines.append(f"{event} {fields!r}")

    info = warning = error = debug = _record


def _settings(token: str | None = TOKEN) -> Settings:
    return Settings(github_api_token=token)


class Sleeps:
    """Records the requested sleeps instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


async def _details(
    pool: asyncpg.Pool | None,
    gh: FakeGitHub | None = None,
    settings: Settings | None = None,
    sleeps: Sleeps | None = None,
    **request: Any,
) -> list[DeployRecord] | ToolFailure:
    request.setdefault("service", SERVICE)
    fake = gh or FakeGitHub()
    async with fake.client() as client:
        return await _get_deploy_details(
            pool,
            settings or _settings(None),
            DeployQuery(**request),
            http_client=client,
            sleep=sleeps or Sleeps(),
        )


def _then_ok(*first: Fault) -> Fault:
    """A fault that answers with *first*, in turn, then serves the PR normally."""
    queue = list(first)

    def fault(request: httpx.Request) -> httpx.Response:
        if queue:
            return queue.pop(0)(request)
        number = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=_pr_body(number), request=request)

    return fault


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


async def _invoke(tool: FunctionTool, arguments: dict[str, Any]) -> str:
    """Call the tool the way the Runner does: JSON arguments through on_invoke_tool.

    Returns the text the model reads back — compact JSON, never a Python repr.
    """
    raw = json.dumps(arguments)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="call-1", tool_arguments=raw)
    text = await tool.on_invoke_tool(ctx, raw)
    assert isinstance(text, str), text
    return text


def _records(result: list[DeployRecord] | ToolFailure) -> list[DeployRecord]:
    assert isinstance(result, list), result
    return result


@pytest.fixture()
def gh() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture()
def log_lines(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorder = RecordingLogger()
    monkeypatch.setattr(deploy_checker, "logger", recorder)
    return recorder.lines


# ── The tool ──────────────────────────────────────────────────────────────────


def test_tool_is_named_get_deploy_details_and_takes_one_request() -> None:
    tool = make_deploy_checker_tool(None, _settings())
    assert isinstance(tool, FunctionTool)
    assert tool.name == "get_deploy_details"
    assert list(tool.params_json_schema["properties"]) == ["request"]
    schema = json.dumps(tool.params_json_schema)
    for field in ("service", "window_hours", "limit"):
        assert field in schema
    assert "repo" not in schema  # the repo is never a model argument


def test_deploy_query_defaults() -> None:
    query = DeployQuery(service=SERVICE)
    assert (query.window_hours, query.limit) == (24, 20)


def test_tool_description_names_failed_deploys_and_failures() -> None:
    description = make_deploy_checker_tool(None, _settings()).description.lower()
    for phrase in ("newest first", "failed", "unavailable", "github"):
        assert phrase in description


async def test_on_invoke_tool_with_json_args(pool: asyncpg.Pool, gh: FakeGitHub) -> None:
    await _insert(pool, ago=timedelta(minutes=20), pr=48, sha="b1c2d3e", status="failed")
    await _insert(pool, ago=timedelta(hours=3), pr=47, sha="a3f9c2e")
    async with gh.client() as client:
        tool = make_deploy_checker_tool(pool, _settings(), http_client=client)
        result = await _invoke(
            tool, {"request": {"service": SERVICE, "window_hours": 2, "limit": 5}}
        )
    items = json.loads(result)
    assert "github_note" not in items[0]  # None fields are left out of the JSON
    records = [DeployRecord.model_validate(item) for item in items]
    assert [r.version for r in records] == ["pr-48-b1c2d3e"]
    assert records[0].deploy_status == "failed"
    assert records[0].github is not None and records[0].github.title == "Change #48"


async def test_on_invoke_tool_defaults_without_a_pool() -> None:
    result = await _invoke(
        make_deploy_checker_tool(None, _settings()), {"request": {"service": SERVICE}}
    )
    data = json.loads(result)
    assert data["error"] is True
    assert (data["tool"], data["kind"]) == ("get_deploy_details", "unavailable")


# ── Input bounds ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "request_fields",
    [
        {"window_hours": 0},
        {"window_hours": 169},
        {"limit": 0},
        {"limit": 51},
        {"service": ""},
        {"service": "   "},
        {"service": "a b; DROP TABLE deployments"},
    ],
)
async def test_bad_input_is_refused(request_fields: dict[str, Any]) -> None:
    result = await _details(None, **request_fields)
    assert isinstance(result, ToolFailure)
    assert (result.tool, result.kind) == ("get_deploy_details", "bad_input")


@pytest.mark.parametrize(
    "fields", [{"window_hours": 1, "limit": 1}, {"window_hours": 168, "limit": 50}]
)
async def test_bounds_are_inclusive(pool: asyncpg.Pool, fields: dict[str, int]) -> None:
    assert await _details(pool, **fields) == []


# ── No database ───────────────────────────────────────────────────────────────


async def test_no_pool_is_unavailable_never_empty() -> None:
    result = await _details(None)
    assert isinstance(result, ToolFailure)
    assert (result.tool, result.kind) == ("get_deploy_details", "unavailable")
    assert "no database" in result.message


async def test_database_failure_is_unavailable() -> None:
    broken = MagicMock()
    broken.acquire = AsyncMock(side_effect=OSError("connection refused"))
    result = await _details(broken)
    assert isinstance(result, ToolFailure)
    assert result.kind == "unavailable"
    assert "connection refused" in result.message


# ── Reading deployments ───────────────────────────────────────────────────────


async def test_newest_first_with_failed_rows_and_all_fields(pool: asyncpg.Pool) -> None:
    await _insert(pool, ago=timedelta(hours=3), pr=40, sha="aaaaaaa", status="succeeded")
    await _insert(
        pool,
        ago=timedelta(minutes=30),
        pr=42,
        sha="ccccccc",
        status="failed",
        stage="verify",
        run=9_000_000_001,
        author="octo",
        files=["app/main.py", "requirements.txt"],
    )
    await _insert(pool, ago=timedelta(hours=1), pr=41, sha="bbbbbbb", status="rolled_back")
    await _insert(pool, ago=timedelta(minutes=10), service="other-service", pr=99)

    records = _records(await _details(pool))

    assert [r.commit_sha for r in records] == ["ccccccc", "bbbbbbb", "aaaaaaa"]
    assert [r.deploy_status for r in records] == ["failed", "rolled_back", "succeeded"]
    newest = records[0]
    assert newest.pr_number == 42
    assert newest.author == "octo"
    assert newest.failed_stage == "verify"
    assert newest.version == "pr-42-ccccccc"
    assert newest.gha_run_id == 9_000_000_001
    assert newest.files_changed == ["app/main.py", "requirements.txt"]
    assert newest.deployed_at.tzinfo is not None
    assert datetime.now(UTC) - newest.deployed_at < timedelta(minutes=31)
    assert records[1].failed_stage == "none"
    assert all(r.github is None for r in records)


async def test_window_filters_older_deploys(pool: asyncpg.Pool) -> None:
    await _insert(pool, ago=timedelta(minutes=30), sha="inside1")
    await _insert(pool, ago=timedelta(minutes=90), sha="inside2")
    await _insert(pool, ago=timedelta(hours=3), sha="outside")
    await _insert(pool, ago=timedelta(hours=30), sha="older")

    two_hours = _records(await _details(pool, window_hours=2))
    day = _records(await _details(pool))  # default 24 h
    week = _records(await _details(pool, window_hours=168))

    assert [r.commit_sha for r in two_hours] == ["inside1", "inside2"]
    assert [r.commit_sha for r in day] == ["inside1", "inside2", "outside"]
    assert [r.commit_sha for r in week] == ["inside1", "inside2", "outside", "older"]


async def test_limit_keeps_the_newest(pool: asyncpg.Pool) -> None:
    for minutes in (50, 10, 40, 20, 30):
        await _insert(pool, ago=timedelta(minutes=minutes), sha=f"m{minutes:06d}")
    records = _records(await _details(pool, limit=2))
    assert [r.commit_sha for r in records] == ["m000010", "m000020"]


async def test_nothing_deployed_is_an_empty_list(pool: asyncpg.Pool) -> None:
    assert await _details(pool) == []
    await _insert(pool, ago=timedelta(hours=5))
    assert await _details(pool, window_hours=1) == []
    assert await _details(pool, service="never-deployed") == []


async def test_direct_push_and_odd_files_column(pool: asyncpg.Pool) -> None:
    await _insert(pool, ago=timedelta(minutes=5), pr=0, files=[], author=None, run=None)
    await pool.execute(
        "UPDATE deployments SET files_changed = NULL, metadata = NULL WHERE pr_number = 0"
    )
    [record] = _records(await _details(pool))
    assert record.pr_number is None  # the pipeline records 0 for a direct push
    assert record.files_changed == []
    assert (record.failed_stage, record.version) == (None, None)
    assert (record.author, record.gha_run_id) == (None, None)


async def test_reads_only(pool: asyncpg.Pool, gh: FakeGitHub) -> None:
    """No INSERT/UPDATE: rows are unchanged and incident_id is never backfilled."""
    await _insert(pool, ago=timedelta(minutes=5), status="failed", stage="deploy")
    before = [dict(r) for r in await pool.fetch("SELECT * FROM deployments ORDER BY id")]
    _records(await _details(pool, gh, settings=_settings()))
    after = [dict(r) for r in await pool.fetch("SELECT * FROM deployments ORDER BY id")]
    assert after == before
    assert all(row["incident_id"] is None for row in after)
    for sql in (deploy_checker._DEPLOYS_SQL, deploy_checker._REPO_SQL):
        assert sql.strip().upper().startswith("SELECT")


# ── The pipeline's own INSERT ─────────────────────────────────────────────────


def test_pipeline_insert_matches_the_workflow() -> None:
    """The INSERT above is the one ci_app_deployment.yml runs (checked when the
    Sentinel-deployment checkout sits next to this repo)."""
    if not _WORKFLOW.is_file():
        pytest.skip("Sentinel-deployment is not checked out next to this repo")
    text = _WORKFLOW.read_text()
    match = re.search(r"INSERT INTO deployments.*?\);", text, re.DOTALL)
    assert match is not None
    assert " ".join(match.group(0).split()) == " ".join(_PIPELINE_INSERT.split())


async def test_row_written_by_the_pipeline_insert_reads_back(pool: asyncpg.Pool) -> None:
    failed = {
        "service": SERVICE,
        "pr": "57",
        "sha": "9f8e7d6",
        "author": "o'brien",
        "status": "failed",
        "run": "11223344556",
        "files": json.dumps(["app/main.py", "app/dir with space/x.py"]),
        "stage": "verify",
        "version": "pr-57-9f8e7d6",
    }
    direct_push = {
        **failed,
        "pr": "0",
        "sha": "1a2b3c4",
        "author": "unknown",
        "status": "succeeded",
        "files": "[]",
        "stage": "none",
        "version": "pr-0-1a2b3c4",
    }
    await pool.execute(_psql_interpolate(_PIPELINE_INSERT, direct_push))
    await pool.execute(_psql_interpolate(_PIPELINE_INSERT, failed))

    records = _records(await _details(pool))

    assert [r.commit_sha for r in records] == ["9f8e7d6", "1a2b3c4"]
    first, second = records
    assert first == DeployRecord(
        pr_number=57,
        commit_sha="9f8e7d6",
        author="o'brien",
        deploy_status="failed",
        deployed_at=first.deployed_at,
        files_changed=["app/main.py", "app/dir with space/x.py"],
        files_changed_total=2,
        failed_stage="verify",
        version="pr-57-9f8e7d6",
        gha_run_id=11223344556,
        github_note="not looked up: GITHUB_API_TOKEN not set",
    )
    assert second.pr_number is None
    assert (second.failed_stage, second.version) == ("none", "pr-0-1a2b3c4")
    assert second.github_note is None  # no PR, nothing to look up


# ── Payload caps ──────────────────────────────────────────────────────────────


async def test_files_changed_keeps_the_first_20_and_the_total(pool: asyncpg.Pool) -> None:
    files = [f"app/module_{i:02d}.py" for i in range(45)]
    await _insert(pool, ago=timedelta(minutes=5), files=files)
    [record] = _records(await _details(pool))
    assert record.files_changed == files[:20]
    assert record.files_changed_total == 45


async def test_a_small_change_lists_every_file(pool: asyncpg.Pool) -> None:
    await _insert(pool, ago=timedelta(minutes=5), files=["a.py", "b.py"])
    [record] = _records(await _details(pool))
    assert (record.files_changed, record.files_changed_total) == (["a.py", "b.py"], 2)


async def test_a_long_author_is_cut_to_100_characters(pool: asyncpg.Pool) -> None:
    await _insert(pool, ago=timedelta(minutes=5), author="x" * 300)
    [record] = _records(await _details(pool))
    assert record.author == "x" * 100


async def test_a_long_github_login_is_cut_to_100_characters(
    pool: asyncpg.Pool, gh: FakeGitHub
) -> None:
    await _insert(pool, ago=timedelta(minutes=5), pr=42)
    body = {**_pr_body(42), "user": {"login": "y" * 300}}
    gh.faults[42] = lambda request: httpx.Response(200, json=body, request=request)
    [record] = _records(await _details(pool, gh, settings=_settings()))
    assert record.github is not None
    assert record.github.author == "y" * 100


# ── GitHub enrichment ─────────────────────────────────────────────────────────


async def test_enrichment_maps_the_pr(pool: asyncpg.Pool, gh: FakeGitHub) -> None:
    await _insert(pool, ago=timedelta(minutes=5), pr=42)
    [record] = _records(await _details(pool, gh, settings=_settings()))
    assert record.github == GitHubPR(
        title="Change #42",
        html_url=f"https://github.com/{REPO}/pull/42",
        merged_at=datetime(2026, 10, 14, 9, 30, tzinfo=UTC),
        author="dev-42",
    )
    assert record.github_note is None


async def test_enrichment_sends_authenticated_gets_to_the_service_repo(
    pool: asyncpg.Pool, gh: FakeGitHub
) -> None:
    await _insert(pool, ago=timedelta(minutes=5), pr=42)
    await _insert(pool, ago=timedelta(minutes=9), pr=0)  # no PR: no request
    _records(await _details(pool, gh, settings=_settings()))
    assert len(gh.requests) == 1
    [request] = gh.requests
    assert request.method == "GET"
    assert str(request.url) == f"https://api.github.com/repos/{REPO}/pulls/42"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert request.headers["Accept"] == "application/vnd.github+json"


async def test_no_token_skips_github(pool: asyncpg.Pool, gh: FakeGitHub) -> None:
    await _insert(pool, ago=timedelta(minutes=5), pr=42)
    [record] = _records(await _details(pool, gh, settings=_settings(None)))
    assert gh.requests == []
    assert record.github is None
    assert record.github_note is not None and "GITHUB_API_TOKEN" in record.github_note


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        (lambda r: httpx.Response(404, json={"message": "Not Found"}, request=r), "HTTP 404"),
        (lambda r: httpx.Response(500, text="boom", request=r), "HTTP 500"),
        (lambda r: httpx.Response(200, text="<html>", request=r), "non-JSON body"),
        (lambda r: httpx.Response(200, json={"message": "?"}, request=r), "unexpected body"),
    ],
)
async def test_failed_lookup_keeps_the_record(
    pool: asyncpg.Pool, gh: FakeGitHub, fault: Fault, reason: str
) -> None:
    await _insert(pool, ago=timedelta(minutes=5), pr=43, sha="bad0000")
    await _insert(pool, ago=timedelta(minutes=9), pr=42, sha="good000")
    gh.faults[43] = fault
    failed, ok = _records(await _details(pool, gh, settings=_settings()))
    assert failed.github is None
    assert failed.github_note == f"GitHub lookup of PR #43 failed: {reason}"
    assert ok.github is not None and ok.github.title == "Change #42"


# ── GitHub retry ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("first", "wait"),
    [
        (lambda r: httpx.Response(503, request=r), 1.0),
        (lambda r: httpx.Response(500, request=r), 1.0),
        (lambda r: httpx.Response(429, headers={"Retry-After": "3"}, request=r), 3.0),
        (lambda r: httpx.Response(429, headers={"Retry-After": "5"}, request=r), 5.0),
        (lambda r: httpx.Response(502, headers={"Retry-After": "soon"}, request=r), 1.0),
        (_timeout, 1.0),
    ],
)
async def test_a_transient_github_failure_is_retried_once(
    pool: asyncpg.Pool, gh: FakeGitHub, first: Fault, wait: float
) -> None:
    await _insert(pool, ago=timedelta(minutes=5), pr=42)
    gh.faults[42] = _then_ok(first)
    sleeps = Sleeps()
    [record] = _records(await _details(pool, gh, settings=_settings(), sleeps=sleeps))
    assert record.github is not None and record.github.title == "Change #42"
    assert record.github_note is None
    assert gh.numbers() == [42, 42]
    assert sleeps.calls == [wait]


async def test_a_second_transient_failure_gives_a_note_not_a_failure(
    pool: asyncpg.Pool, gh: FakeGitHub
) -> None:
    await _insert(pool, ago=timedelta(minutes=5), pr=42)
    gh.faults[42] = lambda r: httpx.Response(503, request=r)
    sleeps = Sleeps()
    [record] = _records(await _details(pool, gh, settings=_settings(), sleeps=sleeps))
    assert record.github is None
    assert record.github_note == "GitHub lookup of PR #42 failed: HTTP 503"
    assert gh.numbers() == [42, 42]  # one retry, no more
    assert sleeps.calls == [1.0]


async def test_a_retry_after_over_5_seconds_is_not_waited_for(
    pool: asyncpg.Pool, gh: FakeGitHub
) -> None:
    await _insert(pool, ago=timedelta(minutes=5), pr=42)
    gh.faults[42] = _then_ok(
        lambda r: httpx.Response(429, headers={"Retry-After": "60"}, request=r)
    )
    sleeps = Sleeps()
    [record] = _records(await _details(pool, gh, settings=_settings(), sleeps=sleeps))
    assert record.github_note == "GitHub lookup of PR #42 failed: HTTP 429"
    assert gh.numbers() == [42]
    assert sleeps.calls == []


@pytest.mark.parametrize("code", [401, 403, 404, 422])
async def test_a_client_error_is_not_retried(pool: asyncpg.Pool, gh: FakeGitHub, code: int) -> None:
    await _insert(pool, ago=timedelta(minutes=5), pr=42)
    gh.faults[42] = _then_ok(lambda r: httpx.Response(code, request=r))
    sleeps = Sleeps()
    [record] = _records(await _details(pool, gh, settings=_settings(), sleeps=sleeps))
    assert record.github_note == f"GitHub lookup of PR #42 failed: HTTP {code}"
    assert gh.numbers() == [42]
    assert sleeps.calls == []


async def test_the_tool_passes_its_sleep_to_the_retry(pool: asyncpg.Pool, gh: FakeGitHub) -> None:
    await _insert(pool, ago=timedelta(minutes=5), pr=42)
    gh.faults[42] = _then_ok(lambda r: httpx.Response(503, request=r))
    sleeps = Sleeps()
    async with gh.client() as client:
        tool = make_deploy_checker_tool(pool, _settings(), http_client=client, sleep=sleeps)
        [item] = json.loads(await _invoke(tool, {"request": {"service": SERVICE}}))
    assert item["github"]["title"] == "Change #42"
    assert sleeps.calls == [1.0]


async def test_transport_error_keeps_the_record(pool: asyncpg.Pool, gh: FakeGitHub) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    await _insert(pool, ago=timedelta(minutes=5), pr=42)
    gh.faults[42] = timeout
    [record] = _records(await _details(pool, gh, settings=_settings()))
    assert record.github is None
    assert record.github_note == "GitHub lookup of PR #42 failed: ConnectTimeout"


async def test_enrichment_is_capped_to_the_newest_prs(pool: asyncpg.Pool, gh: FakeGitHub) -> None:
    for i in range(7):
        await _insert(pool, ago=timedelta(minutes=10 + i), pr=100 + i, sha=f"s{i:06d}")
    records = _records(await _details(pool, gh, settings=_settings()))
    assert sorted(gh.numbers()) == [100, 101, 102, 103, 104]
    assert [r.github is not None for r in records] == [True] * 5 + [False] * 2
    assert all("newest 5" in (r.github_note or "") for r in records[5:])


async def test_a_pr_deployed_twice_is_fetched_once(pool: asyncpg.Pool, gh: FakeGitHub) -> None:
    await _insert(pool, ago=timedelta(minutes=5), pr=42, sha="retry02", status="succeeded")
    await _insert(pool, ago=timedelta(minutes=9), pr=42, sha="retry01", status="failed")
    records = _records(await _details(pool, gh, settings=_settings()))
    assert gh.numbers() == [42]
    assert all(r.github is not None and r.github.title == "Change #42" for r in records)


async def test_service_without_a_repo_is_not_looked_up(pool: asyncpg.Pool, gh: FakeGitHub) -> None:
    await _insert(pool, ago=timedelta(minutes=5), service="no-registry-row", pr=42)
    [record] = _records(await _details(pool, gh, settings=_settings(), service="no-registry-row"))
    assert gh.requests == []
    assert record.github is None
    assert record.github_note == "not looked up: no GitHub repo recorded for no-registry-row"


@pytest.mark.parametrize(
    "repo",
    ["Keshav0375/Sentinel-deployment", "a/b", "o-1/x.y_z", "o/.github", "o/..x", "o/a..b"],
)
def test_repo_pattern_accepts_github_names(repo: str) -> None:
    assert deploy_checker._REPO_RE.fullmatch(repo)


@pytest.mark.parametrize(
    "repo",
    ["o/.", "o/..", "./x", "../x", "o/../x", "o/x/..", "o", "o/", "/x", "-o/x", "o/x y"],
)
def test_repo_pattern_refuses_dot_segments_and_non_names(repo: str) -> None:
    assert deploy_checker._REPO_RE.fullmatch(repo) is None


@pytest.mark.parametrize("repo", ["Keshav0375/..", "Keshav0375/."])
async def test_a_dot_segment_repo_is_not_looked_up(
    pool: asyncpg.Pool, gh: FakeGitHub, repo: str
) -> None:
    await _insert(pool, ago=timedelta(minutes=5), pr=42)
    await pool.execute(
        "UPDATE services SET metadata = jsonb_set(metadata, '{repo}', to_jsonb($2::text)) "
        "WHERE name = $1",
        SERVICE,
        repo,
    )
    try:
        [record] = _records(await _details(pool, gh, settings=_settings()))
    finally:
        await pool.execute(
            "UPDATE services SET metadata = jsonb_set(metadata, '{repo}', to_jsonb($2::text)) "
            "WHERE name = $1",
            SERVICE,
            REPO,
        )
    assert gh.requests == []
    assert record.github_note == f"not looked up: no GitHub repo recorded for {SERVICE}"


async def test_token_never_reaches_logs_results_or_notes(
    pool: asyncpg.Pool,
    gh: FakeGitHub,
    log_lines: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    await _insert(pool, ago=timedelta(minutes=5), pr=41)
    await _insert(pool, ago=timedelta(minutes=6), pr=42)
    await _insert(pool, ago=timedelta(minutes=7), pr=43)

    def echo_auth(request: httpx.Request) -> httpx.Response:
        # A hostile upstream that echoes the Authorization header back.
        return httpx.Response(401, text=request.headers["Authorization"], request=request)

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.headers['Authorization']}", request=request)

    gh.faults[41] = echo_auth
    gh.faults[42] = explode
    results = await _details(pool, gh, settings=_settings())
    unavailable = await _details(None, gh, settings=_settings())

    assert log_lines  # the module did log
    rendered = [
        *log_lines,
        caplog.text,
        repr(results),
        str(results),
        json.dumps([r.model_dump(mode="json") for r in _records(results)]),
        repr(unavailable),
        repr(make_deploy_checker_tool(pool, _settings())),
        repr(_settings()),
    ]
    for text in rendered:
        assert TOKEN not in text


# ── check_rollback_target (the orchestrator's cross-check, not a tool) ────────


async def _recorded_at(pool: asyncpg.Pool, sha: str) -> datetime:
    value = await pool.fetchval("SELECT deployed_at FROM deployments WHERE commit_sha = $1", sha)
    assert isinstance(value, datetime)
    return value


def _target(sha: str, at: datetime, pr: int = 42) -> TargetDeploy:
    return TargetDeploy(pr_number=pr, commit_sha=sha, deployed_at=at)


async def test_target_check_matches_the_row_on_sha_prefix_pr_and_time(
    pool: asyncpg.Pool,
) -> None:
    await _insert(pool, ago=timedelta(minutes=20), sha="a1b2c3d4e5f6", status="failed")
    at = await _recorded_at(pool, "a1b2c3d4e5f6")

    check = await check_rollback_target(pool, SERVICE, _target("A1B2C3D", at))

    assert check.ok and check.problem is None
    assert check.deploy_id is not None and uuid.UUID(check.deploy_id)
    assert check.record is not None
    assert check.record.commit_sha == "a1b2c3d4e5f6"
    assert check.record.deploy_status == "failed"  # failed deploys are rollback targets
    assert check.record.files_changed == ["app/main.py"]


async def test_target_check_accepts_a_full_sha_for_an_abbreviated_row(
    pool: asyncpg.Pool,
) -> None:
    await _insert(pool, ago=timedelta(minutes=5), sha="a1b2c3d")
    at = await _recorded_at(pool, "a1b2c3d")
    full = "a1b2c3d" + "0" * 33
    check = await check_rollback_target(pool, SERVICE, _target(full, at))
    assert check.ok


async def test_target_check_tolerates_a_dropped_sub_second_part(pool: asyncpg.Pool) -> None:
    await _insert(pool, ago=timedelta(minutes=5))
    at = await _recorded_at(pool, "a1b2c3d")
    check = await check_rollback_target(
        pool, SERVICE, _target("a1b2c3d", at.replace(microsecond=0))
    )
    assert check.ok
    late = at + deploy_checker.DEPLOYED_AT_TOLERANCE + timedelta(seconds=1)
    assert not (await check_rollback_target(pool, SERVICE, _target("a1b2c3d", late))).ok


async def test_target_check_names_the_mismatch(pool: asyncpg.Pool) -> None:
    await _insert(pool, ago=timedelta(minutes=5), pr=41)
    at = await _recorded_at(pool, "a1b2c3d")

    wrong_pr = await check_rollback_target(pool, SERVICE, _target("a1b2c3d", at, pr=42))
    assert wrong_pr.record is None and wrong_pr.deploy_id is None
    assert wrong_pr.problem is not None
    assert "recorded as PR #41" in wrong_pr.problem and "says PR #42" in wrong_pr.problem

    missing = await check_rollback_target(pool, SERVICE, _target("ffffffff", at, pr=41))
    assert missing.problem == (
        f"no deployments row for {SERVICE} has commit ffffffff: the rollback target is "
        "not a recorded deploy"
    )


async def test_target_check_reads_only_the_incidents_service(pool: asyncpg.Pool) -> None:
    await _insert(pool, ago=timedelta(minutes=5), service="other-service")
    at = await _recorded_at(pool, "a1b2c3d")
    check = await check_rollback_target(pool, SERVICE, _target("a1b2c3d", at))
    assert check.problem is not None and "no deployments row" in check.problem


async def test_target_check_ignores_a_recorded_sha_under_7_characters(
    pool: asyncpg.Pool,
) -> None:
    await _insert(pool, ago=timedelta(minutes=5), sha="a1b2")
    at = await _recorded_at(pool, "a1b2")
    check = await check_rollback_target(pool, SERVICE, _target("a1b2c3d", at))
    assert not check.ok


async def test_target_check_without_a_database_says_so() -> None:
    check = await check_rollback_target(None, SERVICE, _target("a1b2c3d", datetime.now(UTC)))
    assert check.problem == "the rollback target cannot be checked: no database is configured"


async def test_target_check_reports_a_database_failure() -> None:
    broken = MagicMock()
    broken.acquire = AsyncMock(side_effect=OSError("connection refused"))
    check = await check_rollback_target(broken, SERVICE, _target("a1b2c3d", datetime.now(UTC)))
    assert check.problem is not None
    assert check.problem.startswith("the rollback target cannot be checked:")
