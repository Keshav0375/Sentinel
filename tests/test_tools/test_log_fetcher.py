"""Tests for sentinel.tools.log_fetcher — fetch_logs over the Datadog API.

No network: every request goes to an in-process fake Datadog behind
``httpx.MockTransport``, which records each request so the tests can check the URLs,
the auth headers, the time window and the retries. Sleeps are recorded, never awaited.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from agents import FunctionTool
from agents.tool_context import ToolContext

from sentinel.config import Settings
from sentinel.models.tools import (
    DeployLogEntry,
    LogFetchInput,
    LogFetchResult,
    SyntheticResult,
    ToolFailure,
)
from sentinel.tools import log_fetcher
from sentinel.tools.log_fetcher import _fetch_logs, make_log_fetcher_tool

API_KEY = "not-a-real-api-key"
APP_KEY = "not-a-real-app-key"
SERVICE = "sentinel-watchtower"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)

Fault = Callable[[httpx.Request], httpx.Response]


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _assertions_root() -> list[dict[str, Any]]:
    return [
        {"type": "statusCode", "operator": "is", "target": 200},
        {
            "type": "header",
            "property": "content-type",
            "operator": "contains",
            "target": "application/json",
        },
        {
            "type": "body",
            "operator": "validatesJSONPath",
            "target": {
                "jsonPath": "$.message",
                "operator": "is",
                "elementsOperator": "firstElementMatches",
                "targetValue": "ok",
            },
        },
    ]


TEST_ROOT: dict[str, Any] = {
    "public_id": "abc-root-123",
    "name": "sentinel-runtime-health GET /",
    "type": "api",
    "subtype": "http",
    "status": "live",
    "tags": [f"service:{SERVICE}", "env:dev"],
    "config": {
        "request": {"method": "GET", "url": "https://app.example/"},
        "assertions": _assertions_root(),
    },
}
TEST_HEALTH: dict[str, Any] = {
    "public_id": "abc-health-456",
    "name": "sentinel-runtime-health GET /health",
    "type": "api",
    "subtype": "http",
    "status": "live",
    "tags": [f"service:{SERVICE}", "env:dev"],
    "config": {
        "request": {"method": "GET", "url": "https://app.example/health"},
        "assertions": [{"type": "statusCode", "operator": "is", "target": 200}],
    },
}
TEST_OTHER_SERVICE: dict[str, Any] = {
    "public_id": "zzz-other-000",
    "name": "other GET /",
    "type": "api",
    "tags": ["service:other-service"],
}
TEST_BROWSER: dict[str, Any] = {
    "public_id": "bbb-browser-000",
    "name": "browser journey",
    "type": "browser",
    "tags": [f"service:{SERVICE}"],
}


def _run(result_id: str, at: datetime, *, passed: bool, total: float = 120.0) -> dict[str, Any]:
    return {
        "check_time": float(_ms(at)),
        "probe_dc": "aws:ca-central-1",
        "result": {"passed": passed, "timings": {"total": total, "dns": 1.0}},
        "result_id": result_id,
        "status": 0 if passed else 1,
    }


def _detail(
    result_id: str,
    *,
    failure: dict[str, Any],
    status: int | None = None,
    headers: dict[str, Any] | None = None,
    body: str | None = None,
    assertions: list[dict[str, Any]] | None = None,
    total: float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"eventType": "finished", "failure": failure}
    if status is not None:
        result["httpStatusCode"] = status
    if headers is not None:
        result["responseHeaders"] = headers
    if body is not None:
        result["responseBody"] = body
    if total is not None:
        result["timings"] = {"total": total}
    detail: dict[str, Any] = {"result_id": result_id, "status": 1, "result": result}
    if assertions is not None:
        detail["check"] = {"config": {"assertions": assertions}}
    return detail


def _deploy_log(
    log_id: str,
    at: str,
    *,
    status: str = "info",
    deploy: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {"hostname": "gha-runner"}
    if deploy is not None:
        attributes["deploy"] = deploy
    return {
        "id": log_id,
        "type": "log",
        "attributes": {
            "timestamp": at,
            "status": status,
            "service": SERVICE,
            "message": "deploy.completed",
            "host": "gha-runner",
            "tags": tags or [],
            "attributes": attributes,
        },
    }


LOG_FAILED = _deploy_log(
    "log-2",
    "2026-10-14T11:50:00.000Z",
    status="error",
    deploy={
        "pr_number": 48,
        "version": "pr-48-b1c2d3e",
        "pr_title": "feat: something",
        "status": "failed",
        "failed_stage": "verify",
        "duration_seconds": 97,
    },
    tags=["version:pr-48-b1c2d3e", f"service:{SERVICE}", "deploy_status:failed"],
)
LOG_TAGS_ONLY = _deploy_log(
    "log-1",
    "2026-10-14T11:20:00.000Z",
    tags=[
        "version:pr-47-a3f9c2e",
        f"service:{SERVICE}",
        "env:dev",
        "deploy_status:succeeded",
        "failed_stage:none",
    ],
)


class FakeDatadog:
    """In-process Datadog: routes the four read endpoints, records every request.

    ``faults`` are served first, one per request, before normal routing — a fault
    returns a response or raises an httpx error.
    """

    def __init__(self) -> None:
        self.tests: list[dict[str, Any]] = [
            TEST_ROOT,
            TEST_HEALTH,
            TEST_OTHER_SERVICE,
            TEST_BROWSER,
        ]
        self.results: dict[str, list[dict[str, Any]]] = {
            "abc-root-123": [
                _run("r-root-pass", NOW - timedelta(minutes=35), passed=True, total=88.0),
                _run("r-root-500", NOW - timedelta(minutes=4), passed=False, total=41.5),
                _run("r-root-ct", NOW - timedelta(minutes=10), passed=False, total=52.0),
            ],
            "abc-health-456": [
                _run("r-health-pass", NOW - timedelta(minutes=50), passed=True),
                _run("r-health-timeout", NOW - timedelta(minutes=20), passed=False, total=0),
            ],
        }
        self.details: dict[tuple[str, str], dict[str, Any]] = {
            ("abc-root-123", "r-root-500"): _detail(
                "r-root-500",
                status=500,
                failure={
                    "code": "INCORRECT_ASSERTION",
                    "message": "Assertion failed: statusCode is 200; actual: 500",
                },
                headers={"Content-Type": "application/json"},
                body='{"detail":"Internal Server Error"}',
                assertions=_assertions_root(),
                total=41.5,
            ),
            ("abc-root-123", "r-root-ct"): _detail(
                "r-root-ct",
                status=200,
                failure={
                    "code": "INCORRECT_ASSERTION",
                    "message": "Assertion failed: header content-type contains "
                    "application/json; actual: text/html; charset=utf-8",
                },
                headers={"content-type": "text/html; charset=utf-8", "server": "uvicorn"},
                body="<html><body>ok</body></html>",
            ),
            ("abc-health-456", "r-health-timeout"): _detail(
                "r-health-timeout",
                failure={
                    "code": "TIMEOUT",
                    "message": "Error: Hostname app.example timed out after 60000ms",
                },
            ),
        }
        self.logs: dict[str, Any] = {
            "data": [LOG_FAILED, LOG_TAGS_ONLY],
            "links": {},
            "meta": {"status": "done", "page": {}},
        }
        self.faults: list[Fault] = []
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.faults:
            return self.faults.pop(0)(request)
        path = request.url.path
        parts = path.strip("/").split("/")
        if request.method == "GET" and path == "/api/v1/synthetics/tests":
            return httpx.Response(200, json={"tests": self.tests})
        if request.method == "GET" and parts[:4] == ["api", "v1", "synthetics", "tests"]:
            public_id = parts[4]
            if len(parts) == 6 and parts[5] == "results":
                return httpx.Response(
                    200,
                    json={"last_timestamp_fetched": 0, "results": self.results[public_id]},
                )
            if len(parts) == 7 and parts[5] == "results":
                return httpx.Response(200, json=self.details[(public_id, parts[6])])
        if request.method == "POST" and path == "/api/v2/logs/events/search":
            return httpx.Response(200, json=self.logs)
        return httpx.Response(404, json={"errors": ["Not found"]})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


class Sleeps:
    """Records the requested sleeps instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class RecordingLogger:
    """Stands in for the module's structlog logger; keeps every event as text."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def _record(self, event: str, **fields: Any) -> None:
        self.lines.append(f"{event} {fields!r}")

    info = warning = error = debug = _record


def _status(code: int, **kwargs: Any) -> Fault:
    return lambda request: httpx.Response(code, request=request, **kwargs)


def _raise(exc_type: type[httpx.TransportError], message: str) -> Fault:
    def fault(request: httpx.Request) -> httpx.Response:
        raise exc_type(message, request=request)

    return fault


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"dd_api_key": API_KEY, "dd_app_key": APP_KEY}
    values.update(overrides)
    return Settings(**values)


async def _fetch(
    dd: FakeDatadog,
    sleeps: Sleeps | None = None,
    settings: Settings | None = None,
    **request: Any,
) -> LogFetchResult | ToolFailure:
    request.setdefault("service", SERVICE)
    async with dd.client() as client:
        return await _fetch_logs(
            settings or _settings(),
            LogFetchInput(**request),
            http_client=client,
            sleep=sleeps or Sleeps(),
            clock=lambda: NOW,
        )


async def _fetch_with(
    handler: Callable[[httpx.Request], Awaitable[httpx.Response]],
    *,
    deadline: float = 45.0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    **request: Any,
) -> LogFetchResult | ToolFailure:
    """``_fetch`` over an async handler (one that can stall) with a chosen deadline."""
    request.setdefault("service", SERVICE)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        return await _fetch_logs(
            _settings(),
            LogFetchInput(**request),
            http_client=client,
            sleep=sleep or Sleeps(),
            clock=lambda: NOW,
            deadline=deadline,
        )


def _result(result: LogFetchResult | ToolFailure) -> LogFetchResult:
    assert isinstance(result, LogFetchResult), result
    return result


async def _invoke(tool: FunctionTool, arguments: dict[str, Any]) -> str:
    """Call the tool the way the Runner does: JSON arguments through on_invoke_tool.

    Returns the text the model reads back — compact JSON, never a Python repr.
    """
    raw = json.dumps(arguments)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="call-1", tool_arguments=raw)
    text = await tool.on_invoke_tool(ctx, raw)
    assert isinstance(text, str), text
    return text


@pytest.fixture()
def dd() -> FakeDatadog:
    return FakeDatadog()


@pytest.fixture()
def log_lines(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorder = RecordingLogger()
    monkeypatch.setattr(log_fetcher, "logger", recorder)
    return recorder.lines


# ── The tool ──────────────────────────────────────────────────────────────────


def test_tool_is_named_fetch_logs_and_takes_one_request() -> None:
    tool = make_log_fetcher_tool(_settings())
    assert isinstance(tool, FunctionTool)
    assert tool.name == "fetch_logs"
    assert list(tool.params_json_schema["properties"]) == ["request"]
    schema = json.dumps(tool.params_json_schema)
    for field in ("service", "window_minutes", "include", "synthetics", "deploys"):
        assert field in schema


def test_tool_description_names_both_sources_and_failures() -> None:
    description = make_log_fetcher_tool(_settings()).description.lower()
    assert "synthetics" in description
    assert "deploy.completed" in description
    assert "no finding" in description


async def test_invoked_with_json_args_returns_the_typed_result(dd: FakeDatadog) -> None:
    async with dd.client() as client:
        tool = make_log_fetcher_tool(
            _settings(), http_client=client, sleep=Sleeps(), clock=lambda: NOW
        )
        result = await _invoke(
            tool,
            {"request": {"service": SERVICE, "window_minutes": 30, "include": ["deploys"]}},
        )
    assert "deploy.completed" in result
    result = LogFetchResult.model_validate_json(result)
    assert result.sources == {"deploys": "ok"}
    assert result.window.start == NOW - timedelta(minutes=30)
    assert [e.version for e in result.deploy_logs] == ["pr-48-b1c2d3e", "pr-47-a3f9c2e"]
    assert dd.paths() == ["POST /api/v2/logs/events/search"]


async def test_invoked_with_defaults_reads_both_sources(dd: FakeDatadog) -> None:
    async with dd.client() as client:
        tool = make_log_fetcher_tool(
            _settings(), http_client=client, sleep=Sleeps(), clock=lambda: NOW
        )
        text = await _invoke(tool, {"request": {"service": SERVICE}})
    result = LogFetchResult.model_validate_json(text)
    assert result.sources == {"synthetics": "ok", "deploys": "ok"}
    assert result.window.end - result.window.start == timedelta(minutes=60)
    assert len(result.synthetic_results) == 5
    assert len(result.deploy_logs) == 2


# ── URLs from dd_site ─────────────────────────────────────────────────────────


async def test_urls_use_the_default_us5_site(dd: FakeDatadog) -> None:
    result = await _fetch(dd)
    assert isinstance(result, LogFetchResult)
    assert Settings.model_fields["dd_site"].default == "us5.datadoghq.com"
    assert {r.url.host for r in dd.requests} == {"api.us5.datadoghq.com"}
    assert all(r.url.scheme == "https" for r in dd.requests)


async def test_urls_use_a_custom_site(dd: FakeDatadog) -> None:
    result = await _fetch(dd, settings=_settings(dd_site="datadoghq.eu"))
    assert isinstance(result, LogFetchResult)
    assert {r.url.host for r in dd.requests} == {"api.datadoghq.eu"}


async def test_a_site_that_is_not_a_hostname_is_refused(dd: FakeDatadog) -> None:
    result = await _fetch(dd, settings=_settings(dd_site="evil.example/steal?x="))
    assert isinstance(result, ToolFailure)
    assert result.kind == "unavailable"
    assert "dd_site" in result.message
    assert dd.requests == []


# ── Auth and key hygiene ──────────────────────────────────────────────────────


async def test_every_request_carries_both_auth_headers(dd: FakeDatadog) -> None:
    await _fetch(dd)
    assert dd.requests
    for request in dd.requests:
        assert request.headers["DD-API-KEY"] == API_KEY
        assert request.headers["DD-APPLICATION-KEY"] == APP_KEY


async def test_only_reads_are_sent(dd: FakeDatadog) -> None:
    await _fetch(dd)
    non_get = {f"{r.method} {r.url.path}" for r in dd.requests if r.method != "GET"}
    assert non_get == {"POST /api/v2/logs/events/search"}


async def test_keys_never_reach_logs_results_or_failures(
    dd: FakeDatadog, log_lines: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    ok = await _fetch(dd)
    dd.faults = [_status(503), _status(503), _status(503)]
    exhausted = await _fetch(dd)
    dd.faults = [_status(403, json={"errors": ["Forbidden"]})]
    forbidden = await _fetch(dd)
    dd.faults = [_raise(httpx.ConnectError, "connection refused")] * 3
    unreachable = await _fetch(dd)
    missing = await _fetch(dd, settings=_settings(dd_app_key=None))

    assert isinstance(ok, LogFetchResult)
    assert log_lines  # the module did log
    rendered = [
        *log_lines,
        caplog.text,
        *(repr(r) for r in (ok, exhausted, forbidden, unreachable, missing)),
        *(str(r) for r in (ok, exhausted, forbidden, unreachable, missing)),
        *(r.model_dump_json() for r in (ok, exhausted, forbidden, unreachable, missing)),
        repr(make_log_fetcher_tool(_settings())),
        repr(_settings()),
    ]
    for text in rendered:
        assert API_KEY not in text
        assert APP_KEY not in text


@pytest.mark.parametrize(
    ("overrides", "named", "not_named"),
    [
        ({"dd_api_key": None}, "dd_api_key (DD_API_KEY)", "dd_app_key"),
        ({"dd_app_key": None}, "dd_app_key (DD_APP_KEY)", "dd_api_key"),
        ({"dd_api_key": "  ", "dd_app_key": None}, "dd_api_key (DD_API_KEY)", None),
    ],
)
async def test_a_missing_key_is_unavailable_and_named(
    dd: FakeDatadog, overrides: dict[str, Any], named: str, not_named: str | None
) -> None:
    result = await _fetch(dd, settings=_settings(**overrides))
    assert isinstance(result, ToolFailure)
    assert result.tool == "fetch_logs"
    assert result.kind == "unavailable"
    assert named in result.message
    if not_named:
        assert not_named not in result.message
    assert API_KEY not in result.message and APP_KEY not in result.message
    assert dd.requests == []


async def test_no_keys_at_all_names_both(dd: FakeDatadog) -> None:
    result = await _fetch(dd, settings=_settings(dd_api_key=None, dd_app_key=None))
    assert isinstance(result, ToolFailure)
    assert "dd_api_key (DD_API_KEY)" in result.message
    assert "dd_app_key (DD_APP_KEY)" in result.message


# ── Synthetics ────────────────────────────────────────────────────────────────


async def test_only_the_services_api_tests_are_read(dd: FakeDatadog) -> None:
    await _fetch(dd, include=["synthetics"])
    paths = dd.paths()
    assert paths[0] == "GET /api/v1/synthetics/tests"
    assert "GET /api/v1/synthetics/tests/abc-root-123/results" in paths
    assert "GET /api/v1/synthetics/tests/abc-health-456/results" in paths
    assert not any("zzz-other" in p or "bbb-browser" in p for p in paths)
    assert "POST /api/v2/logs/events/search" not in paths


async def test_runs_come_back_newest_first_with_summary_fields(dd: FakeDatadog) -> None:
    result = await _fetch(dd, include=["synthetics"])
    assert isinstance(result, LogFetchResult)
    runs = result.synthetic_results
    assert [(r.public_id, r.passed) for r in runs] == [
        ("abc-root-123", False),
        ("abc-root-123", False),
        ("abc-health-456", False),
        ("abc-root-123", True),
        ("abc-health-456", True),
    ]
    assert [r.checked_at for r in runs] == [NOW - timedelta(minutes=m) for m in (4, 10, 20, 35, 50)]
    passing = runs[3]
    assert passing.test_name == "sentinel-runtime-health GET /"
    assert passing.location == "aws:ca-central-1"
    assert passing.latency_ms == 88.0
    assert passing.status_code is None and passing.failure_code is None
    assert passing.assertions == []
    assert result.truncated is False


async def test_a_failing_get_root_with_500(dd: FakeDatadog) -> None:
    result = await _fetch(dd, include=["synthetics"])
    assert isinstance(result, LogFetchResult)
    run = result.synthetic_results[0]
    assert run == SyntheticResult(
        test_name="sentinel-runtime-health GET /",
        public_id="abc-root-123",
        checked_at=NOW - timedelta(minutes=4),
        passed=False,
        location="aws:ca-central-1",
        latency_ms=41.5,
        status_code=500,
        failure_code="INCORRECT_ASSERTION",
        failure_message="Assertion failed: statusCode is 200; actual: 500",
        content_type="application/json",
        body_snippet='{"detail":"Internal Server Error"}',
        assertions=[
            "statusCode is 200",
            "header content-type contains application/json",
            "body validatesJSONPath $.message is ok",
        ],
    )


async def test_a_content_type_failure_uses_the_tests_own_assertions(dd: FakeDatadog) -> None:
    result = await _fetch(dd, include=["synthetics"])
    assert isinstance(result, LogFetchResult)
    run = result.synthetic_results[1]
    assert run.status_code == 200
    assert run.content_type == "text/html; charset=utf-8"
    assert run.body_snippet == "<html><body>ok</body></html>"
    assert run.failure_message is not None and "content-type" in run.failure_message
    # The detail carried no check: the assertions come from the test definition.
    assert "header content-type contains application/json" in run.assertions
    assert run.latency_ms == 52.0  # no timings in the detail: the summary's stays


async def test_a_timeout_has_no_status_code(dd: FakeDatadog) -> None:
    result = await _fetch(dd, include=["synthetics"])
    assert isinstance(result, LogFetchResult)
    run = result.synthetic_results[2]
    assert run.test_name == "sentinel-runtime-health GET /health"
    assert run.passed is False
    assert run.status_code is None
    assert run.failure_code == "TIMEOUT"
    assert run.failure_message is not None and "timed out" in run.failure_message
    assert run.content_type is None and run.body_snippet is None
    assert run.assertions == ["statusCode is 200"]


async def test_details_are_fetched_for_failed_runs_only(dd: FakeDatadog) -> None:
    await _fetch(dd, include=["synthetics"])
    details = [p for p in dd.paths() if p.count("/") == 7]
    assert sorted(details) == [
        "GET /api/v1/synthetics/tests/abc-health-456/results/r-health-timeout",
        "GET /api/v1/synthetics/tests/abc-root-123/results/r-root-500",
        "GET /api/v1/synthetics/tests/abc-root-123/results/r-root-ct",
    ]


async def test_no_tests_for_the_service_is_an_empty_list(dd: FakeDatadog) -> None:
    dd.tests = [TEST_OTHER_SERVICE]
    result = await _fetch(dd, include=["synthetics"])
    assert isinstance(result, LogFetchResult)
    assert result.sources == {"synthetics": "ok"}
    assert result.synthetic_results == []
    assert dd.paths() == ["GET /api/v1/synthetics/tests"]


# ── CI pipeline logs ──────────────────────────────────────────────────────────


async def test_deploy_logs_are_parsed(dd: FakeDatadog) -> None:
    result = await _fetch(dd, include=["deploys"])
    assert isinstance(result, LogFetchResult)
    assert result.deploy_logs == [
        DeployLogEntry(
            timestamp=datetime(2026, 10, 14, 11, 50, tzinfo=UTC),
            status="error",
            version="pr-48-b1c2d3e",
            deploy_status="failed",
            failed_stage="verify",
            pr_number=48,
            message="deploy.completed",
        ),
        # No deploy attribute: the fields come from the tags; no PR number there.
        DeployLogEntry(
            timestamp=datetime(2026, 10, 14, 11, 20, tzinfo=UTC),
            status="info",
            version="pr-47-a3f9c2e",
            deploy_status="succeeded",
            failed_stage="none",
            pr_number=None,
            message="deploy.completed",
        ),
    ]
    # The PR title (free text from the PR author) is not passed to the model.
    assert "feat: something" not in result.model_dump_json()


async def test_no_deploy_logs_is_an_empty_list(dd: FakeDatadog) -> None:
    dd.logs = {"data": [], "meta": {"status": "done", "page": {}}}
    result = await _fetch(dd, include=["deploys"])
    assert isinstance(result, LogFetchResult)
    assert result.deploy_logs == []
    assert result.truncated is False


# ── Time window ───────────────────────────────────────────────────────────────


async def test_the_window_is_sent_to_both_apis(dd: FakeDatadog) -> None:
    result = await _fetch(dd, window_minutes=90)
    assert isinstance(result, LogFetchResult)
    start = NOW - timedelta(minutes=90)
    assert result.window.start == start and result.window.end == NOW

    results_calls = [r for r in dd.requests if r.url.path.endswith("/results")]
    assert len(results_calls) == 2
    for request in results_calls:
        assert request.url.params["from_ts"] == str(_ms(start))
        assert request.url.params["to_ts"] == str(_ms(NOW))

    (search,) = [r for r in dd.requests if r.method == "POST"]
    body = json.loads(search.content)
    assert body == {
        "filter": {
            "query": f'service:{SERVICE} (@deploy.status:* OR "deploy.completed")',
            "from": "2026-10-14T10:30:00.000Z",
            "to": "2026-10-14T12:00:00.000Z",
        },
        "sort": "-timestamp",
        "page": {"limit": 20},
    }
    assert search.headers["content-type"] == "application/json"


@pytest.mark.parametrize("minutes", [0, -5, 1441, 100_000])
async def test_a_window_out_of_bounds_is_bad_input(dd: FakeDatadog, minutes: int) -> None:
    result = await _fetch(dd, window_minutes=minutes)
    assert isinstance(result, ToolFailure)
    assert result.kind == "bad_input"
    assert "window_minutes" in result.message
    assert dd.requests == []


@pytest.mark.parametrize("minutes", [1, 1440])
async def test_the_window_bounds_are_inclusive(dd: FakeDatadog, minutes: int) -> None:
    result = await _fetch(dd, window_minutes=minutes)
    assert isinstance(result, LogFetchResult)
    assert result.window.end - result.window.start == timedelta(minutes=minutes)


@pytest.mark.parametrize("service", ["", "   ", "svc OR service:*", 'a"b', "x" * 101])
async def test_a_bad_service_name_is_bad_input(dd: FakeDatadog, service: str) -> None:
    result = await _fetch(dd, service=service)
    assert isinstance(result, ToolFailure)
    assert result.kind == "bad_input"
    assert dd.requests == []


async def test_an_empty_include_is_bad_input(dd: FakeDatadog) -> None:
    result = await _fetch(dd, include=[])
    assert isinstance(result, ToolFailure)
    assert result.kind == "bad_input"


# ── Retries ───────────────────────────────────────────────────────────────────


async def test_a_503_is_retried_then_succeeds(dd: FakeDatadog) -> None:
    sleeps = Sleeps()
    dd.faults = [_status(503)]
    result = await _fetch(dd, sleeps, include=["deploys"])
    assert isinstance(result, LogFetchResult)
    assert len(result.deploy_logs) == 2
    assert dd.paths() == ["POST /api/v2/logs/events/search"] * 2
    assert sleeps.calls == [1.0]


async def test_a_timeout_is_retried_then_succeeds(dd: FakeDatadog) -> None:
    sleeps = Sleeps()
    dd.faults = [_raise(httpx.ReadTimeout, "read timed out"), _status(502)]
    result = await _fetch(dd, sleeps, include=["synthetics"])
    assert isinstance(result, LogFetchResult)
    assert len(result.synthetic_results) == 5
    assert dd.paths()[:3] == ["GET /api/v1/synthetics/tests"] * 3
    assert sleeps.calls == [1.0, 2.0]


async def test_a_connect_error_is_retried(dd: FakeDatadog) -> None:
    sleeps = Sleeps()
    dd.faults = [_raise(httpx.ConnectError, "connection refused")]
    result = await _fetch(dd, sleeps, include=["deploys"])
    assert isinstance(result, LogFetchResult)
    assert sleeps.calls == [1.0]


async def test_three_transient_failures_give_upstream_error(dd: FakeDatadog) -> None:
    sleeps = Sleeps()
    dd.faults = [_status(503), _raise(httpx.ConnectTimeout, "connect timed out"), _status(500)]
    result = await _fetch(dd, sleeps, include=["deploys"])
    assert isinstance(result, ToolFailure)
    assert result.kind == "upstream_error"
    assert "after 3 attempts" in result.message
    assert "HTTP 500" in result.message
    assert len(dd.requests) == 3
    assert sleeps.calls == [1.0, 2.0]


async def test_a_403_is_not_retried(dd: FakeDatadog) -> None:
    sleeps = Sleeps()
    dd.faults = [_status(403, json={"errors": ["Forbidden"]})]
    result = await _fetch(dd, sleeps, include=["synthetics"])
    assert isinstance(result, ToolFailure)
    assert result.kind == "upstream_error"
    assert "HTTP 403" in result.message
    assert "Forbidden" in result.message
    assert len(dd.requests) == 1
    assert sleeps.calls == []


async def test_a_400_on_the_search_is_not_retried(dd: FakeDatadog) -> None:
    sleeps = Sleeps()
    dd.faults = [_status(400, json={"errors": ["invalid query"]})]
    result = await _fetch(dd, sleeps, include=["deploys"])
    assert isinstance(result, ToolFailure)
    assert result.kind == "upstream_error"
    assert "/api/v2/logs/events/search" in result.message
    assert "invalid query" in result.message
    assert len(dd.requests) == 1


async def test_a_failure_in_one_source_returns_the_others(dd: FakeDatadog) -> None:
    """The deploy logs still come back; the failed source says so in ``sources``."""
    dd.faults = [_status(403, json={"errors": ["Forbidden"]})]  # the synthetics listing
    result = _result(await _fetch(dd))
    assert result.sources["deploys"] == "ok"
    status = result.sources["synthetics"]
    assert status.startswith("failed: upstream_error: ")
    assert "/api/v1/synthetics/tests" in status and "HTTP 403" in status
    assert result.synthetic_results == []  # not read — not "nothing recorded"
    assert [e.version for e in result.deploy_logs] == ["pr-48-b1c2d3e", "pr-47-a3f9c2e"]


async def test_a_failed_deploy_search_keeps_the_synthetics(dd: FakeDatadog) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(400, json={"errors": ["invalid query"]}, request=request)
        return dd.handler(request)

    result = _result(await _fetch_with(handler))
    assert result.sources["synthetics"] == "ok"
    assert result.sources["deploys"].startswith("failed: upstream_error: ")
    assert "invalid query" in result.sources["deploys"]
    assert len(result.synthetic_results) == 5
    assert result.deploy_logs == []


async def test_every_source_failing_is_a_tool_failure(dd: FakeDatadog) -> None:
    dd.faults = [_status(403, json={"errors": ["Forbidden"]})] * 2
    result = await _fetch(dd)
    assert isinstance(result, ToolFailure)
    assert result.kind == "upstream_error"
    assert "synthetics: " in result.message and "deploys: " in result.message


async def test_a_partial_result_reaches_the_model_as_json(dd: FakeDatadog) -> None:
    dd.faults = [_status(403, json={"errors": ["Forbidden"]})]
    async with dd.client() as client:
        tool = make_log_fetcher_tool(
            _settings(), http_client=client, sleep=Sleeps(), clock=lambda: NOW
        )
        data = json.loads(await _invoke(tool, {"request": {"service": SERVICE}}))
    assert "error" not in data
    assert data["sources"]["deploys"] == "ok"
    assert data["sources"]["synthetics"].startswith("failed: upstream_error")


# ── Overall deadline ──────────────────────────────────────────────────────────


def test_the_default_deadline_is_45_seconds() -> None:
    for fn in (make_log_fetcher_tool, _fetch_logs):
        assert inspect.signature(fn).parameters["deadline"].default == 45.0


async def test_a_source_past_the_deadline_is_failed_and_the_rest_returned(
    dd: FakeDatadog,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            await asyncio.sleep(30)  # the logs search never answers in time
        return dd.handler(request)

    started = time.monotonic()
    result = _result(await _fetch_with(handler, deadline=0.2))
    assert time.monotonic() - started < 5
    assert result.sources == {
        "synthetics": "ok",
        "deploys": "failed: upstream_error: no answer within the 0.2 s deadline",
    }
    assert len(result.synthetic_results) == 5
    assert result.deploy_logs == []


async def test_the_deadline_covers_retries_and_backoff(dd: FakeDatadog) -> None:
    """Real sleeps between retries count against the one deadline for the call."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    started = time.monotonic()
    result = await _fetch_with(handler, deadline=0.2, sleep=asyncio.sleep)
    assert time.monotonic() - started < 5
    assert isinstance(result, ToolFailure)
    assert result.kind == "upstream_error"
    assert result.message.count("no answer within the 0.2 s deadline") == 2


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "3"}, 3.0),
        ({"Retry-After": "600"}, 10.0),  # bounded
        ({"X-RateLimit-Reset": "5"}, 5.0),
        ({"Retry-After": "soon"}, 1.0),  # unparseable: the backoff
        ({}, 1.0),
    ],
)
async def test_a_429_honours_retry_after_within_bounds(
    dd: FakeDatadog, headers: dict[str, str], expected: float
) -> None:
    sleeps = Sleeps()
    dd.faults = [_status(429, headers=headers)]
    result = await _fetch(dd, sleeps, include=["deploys"])
    assert isinstance(result, LogFetchResult)
    assert sleeps.calls == [expected]


async def test_a_non_json_body_is_upstream_error(dd: FakeDatadog) -> None:
    dd.faults = [lambda r: httpx.Response(200, text="<html>maintenance</html>")]
    result = await _fetch(dd, include=["deploys"])
    assert isinstance(result, ToolFailure)
    assert result.kind == "upstream_error"


# ── Truncation ────────────────────────────────────────────────────────────────


async def test_runs_beyond_the_cap_are_dropped_and_flagged(dd: FakeDatadog) -> None:
    dd.results["abc-root-123"] = [
        _run(f"r-{i}", NOW - timedelta(minutes=i + 1), passed=True) for i in range(30)
    ]
    dd.results["abc-health-456"] = []
    result = await _fetch(dd, include=["synthetics"])
    assert isinstance(result, LogFetchResult)
    assert len(result.synthetic_results) == 20
    assert result.synthetic_results[0].checked_at == NOW - timedelta(minutes=1)
    assert result.truncated is True


async def test_failure_details_are_capped(dd: FakeDatadog) -> None:
    runs = [_run(f"f-{i}", NOW - timedelta(minutes=i + 1), passed=False) for i in range(8)]
    dd.results["abc-root-123"] = runs
    dd.results["abc-health-456"] = []
    for i in range(8):
        dd.details[("abc-root-123", f"f-{i}")] = _detail(
            f"f-{i}", status=500, failure={"code": "INCORRECT_ASSERTION", "message": "m"}
        )
    result = await _fetch(dd, include=["synthetics"])
    assert isinstance(result, LogFetchResult)
    assert [r.status_code for r in result.synthetic_results] == [500] * 5 + [None] * 3
    assert len([p for p in dd.paths() if p.count("/") == 7]) == 5
    assert result.truncated is True


async def test_long_bodies_and_messages_are_cut(dd: FakeDatadog) -> None:
    dd.details[("abc-root-123", "r-root-500")] = _detail(
        "r-root-500",
        status=500,
        failure={"code": "INCORRECT_ASSERTION", "message": "y" * 1000},
        body="x" * 5000,
    )
    result = await _fetch(dd, include=["synthetics"])
    assert isinstance(result, LogFetchResult)
    run = result.synthetic_results[0]
    assert run.body_snippet == "x" * 500
    assert run.failure_message == "y" * 300
    assert result.truncated is True


async def test_more_deploy_logs_than_the_page_is_flagged(dd: FakeDatadog) -> None:
    dd.logs["meta"] = {"status": "done", "page": {"after": "cursor-abc"}}
    result = await _fetch(dd, include=["deploys"])
    assert isinstance(result, LogFetchResult)
    assert result.truncated is True


async def test_a_timed_out_search_is_flagged(dd: FakeDatadog) -> None:
    dd.logs["meta"] = {"status": "timeout", "page": {}}
    result = await _fetch(dd, include=["deploys"])
    assert isinstance(result, LogFetchResult)
    assert result.truncated is True


async def test_the_whole_result_stays_small(dd: FakeDatadog) -> None:
    """Worst case at the caps: the payload the model sees stays bounded."""
    dd.results["abc-root-123"] = [
        _run(f"f-{i}", NOW - timedelta(minutes=i + 1), passed=False) for i in range(150)
    ]
    for i in range(150):
        dd.details[("abc-root-123", f"f-{i}")] = _detail(
            f"f-{i}",
            status=500,
            failure={"code": "INCORRECT_ASSERTION", "message": "m" * 5000},
            body="b" * 50_000,
            headers={"content-type": "c" * 5000},
        )
    dd.logs["data"] = [LOG_FAILED] * 20
    result = await _fetch(dd)
    assert isinstance(result, LogFetchResult)
    assert len(result.model_dump_json()) < 20_000
    assert result.truncated is True


async def test_odd_fields_in_a_run_do_not_break_the_call(dd: FakeDatadog) -> None:
    dd.results["abc-root-123"] = [
        {"check_time": 1e30, "result": "not-an-object", "result_id": 7},
        {"check_time": float(_ms(NOW)) / 1000, "result": {"passed": True}},  # seconds
    ]
    dd.results["abc-health-456"] = []
    result = await _fetch(dd, include=["synthetics"])
    assert isinstance(result, LogFetchResult)
    assert [r.checked_at for r in result.synthetic_results] == [None, NOW]
    assert [r.passed for r in result.synthetic_results] == [False, True]
