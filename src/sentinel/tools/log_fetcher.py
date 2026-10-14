"""fetch_logs tool — the Datadog evidence for a service (backend.md §4.8).

The watched app sends no runtime logs, so the evidence is what Datadog really holds
(decision 2026-10-14, "Backend phase 3 (tools) contract"):

- **Synthetics** — the service's API-test runs (``GET /api/v1/synthetics/tests``, its
  ``/results`` and, for failed runs, the result detail): pass/fail, latency, status code,
  failure, content type and a body snippet.
- **CI pipeline logs** — the ``deploy.completed`` logs the deploy workflow sends
  (``POST /api/v2/logs/events/search``; the search POST is a read).

Read-only. Every URL is built from ``settings.dd_site``. Transient errors (connect
errors, timeouts, 429, 5xx) are retried — 3 attempts, 1 s then 2 s apart (429 honours
Retry-After up to a bound); any other error status fails at once. The keys are read only
when the request headers are built and never logged. Nothing is captured to Langfuse.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote

import httpx
from agents import FunctionTool, function_tool

from sentinel.config import Settings, secret_value
from sentinel.infra.logging import get_logger
from sentinel.models.tools import (
    DeployLogEntry,
    LogFetchInput,
    LogFetchResult,
    LogSource,
    LogWindow,
    SyntheticResult,
    ToolFailure,
    ToolFailureKind,
)

logger = get_logger("sentinel.tools.log_fetcher")

_TOOL_NAME = "fetch_logs"

SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], datetime]

# ── Bounds ────────────────────────────────────────────────────────────────────
_MIN_WINDOW_MINUTES = 1
_MAX_WINDOW_MINUTES = 1440

# Payload caps — the result goes to the model, so it stays small.
_MAX_TESTS = 10  # synthetics tests read per call
_MAX_SYNTHETIC_RESULTS = 20  # runs returned, newest first
_MAX_FAILURE_DETAILS = 5  # failed runs whose detail is fetched, newest first
_MAX_DEPLOY_LOGS = 20  # page.limit of the logs search
_MAX_ASSERTIONS = 10
_BODY_SNIPPET_CHARS = 500
_MESSAGE_CHARS = 300
_ERROR_TEXT_CHARS = 200

# ── Retry (CONVENTIONS.md: 3 attempts, 1s/2s/4s) ──────────────────────────────
_ATTEMPTS = 3
_RETRY_DELAYS = (1.0, 2.0, 4.0)
_MAX_RETRY_AFTER = 10.0  # a 429's Retry-After is honoured up to this many seconds
_REQUEST_TIMEOUT = httpx.Timeout(10.0)

# A bare hostname only: the site is spliced into every URL.
_SITE_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
# The service is spliced into a log search query and compared with a tag.
_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# Datadog keys: (Settings field, env var).
_KEYS = (("dd_api_key", "DD_API_KEY"), ("dd_app_key", "DD_APP_KEY"))


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _DatadogFailure(Exception):
    """A Datadog request that failed for good; becomes the call's ToolFailure."""

    def __init__(self, message: str, kind: ToolFailureKind = "upstream_error") -> None:
        super().__init__(message)
        self.kind: ToolFailureKind = kind
        self.message = message


def make_log_fetcher_tool(
    settings: Settings,
    *,
    http_client: httpx.AsyncClient | None = None,
    sleep: SleepFn = asyncio.sleep,
    clock: ClockFn = _utcnow,
) -> FunctionTool:
    """Create the fetch_logs tool with the Datadog settings injected.

    Args:
        settings: Supplies ``dd_api_key``, ``dd_app_key`` and ``dd_site``; the keys are
            read at call time, so a missing key fails the call, never the build.
        http_client: Client to send requests with (tests pass one over a mock
            transport). None → a short-lived client per call.
        sleep: Awaited between retry attempts (tests pass a recorder).
        clock: Returns the current UTC time; the window ends there.

    Returns:
        FunctionTool that agents can call to read a service's Datadog evidence.
    """

    @function_tool(strict_mode=False)
    async def fetch_logs(request: LogFetchInput) -> LogFetchResult | ToolFailure:
        """Fetch a service's Datadog evidence for the last window_minutes, newest first.

        The service sends no application logs. The evidence is (1) its Datadog
        Synthetics check runs — pass/fail, latency and, for failed runs, the HTTP status,
        the failure message, the response content type, a body snippet and what the
        test asserts — and (2) the CI pipeline's deploy.completed logs (version,
        deploy_status, failed_stage, PR number). An empty list for a source in
        ``sources`` means nothing was recorded in the window. A structured failure means
        Datadog could not be read (kind "unavailable": not configured; "upstream_error":
        Datadog answered with an error) — no finding either way.

        Args:
            request: The service, the window in minutes, and which sources to read.
        """
        return await _fetch_logs(
            settings, request, http_client=http_client, sleep=sleep, clock=clock
        )

    return fetch_logs


async def _fetch_logs(
    settings: Settings,
    request: LogFetchInput,
    *,
    http_client: httpx.AsyncClient | None = None,
    sleep: SleepFn = asyncio.sleep,
    clock: ClockFn = _utcnow,
) -> LogFetchResult | ToolFailure:
    """Read the service's Datadog evidence, or return a ToolFailure — never raise.

    Separated from the tool decorator so tests can call it without a ToolContext.
    """
    service = request.service.strip()
    if not _SERVICE_RE.fullmatch(service):
        return _failure("bad_input", f"service {service!r} is not a service name")
    minutes = request.window_minutes
    if not _MIN_WINDOW_MINUTES <= minutes <= _MAX_WINDOW_MINUTES:
        return _failure(
            "bad_input",
            f"window_minutes must be {_MIN_WINDOW_MINUTES}-{_MAX_WINDOW_MINUTES}, got {minutes}",
        )
    sources = list(dict.fromkeys(request.include))
    if not sources:
        return _failure("bad_input", 'include is empty: name "synthetics" and/or "deploys"')

    missing = [f"{field} ({env})" for field, env in _KEYS if getattr(settings, field) is None]
    if missing:
        return _failure("unavailable", f"Datadog is not configured: {', '.join(missing)} not set")
    site = settings.dd_site.strip()
    if not _SITE_RE.fullmatch(site):
        return _failure("unavailable", f"dd_site {site!r} is not a Datadog site hostname")

    end = clock().astimezone(UTC)
    window = LogWindow(start=end - timedelta(minutes=minutes), end=end)
    reader = _DatadogReader(
        base_url=f"https://api.{site}",
        headers=_auth_headers(settings),
        sleep=sleep,
    )
    try:
        if http_client is None:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                result = await reader.collect(client, service, window, sources)
        else:
            result = await reader.collect(http_client, service, window, sources)
    except _DatadogFailure as exc:
        logger.warning("fetch_logs_failed", service=service, kind=exc.kind, error=exc.message)
        return _failure(exc.kind, exc.message)

    logger.info(
        "fetch_logs_done",
        service=service,
        window_minutes=minutes,
        synthetic_results=len(result.synthetic_results),
        deploy_logs=len(result.deploy_logs),
        truncated=result.truncated,
    )
    return result


def _failure(kind: ToolFailureKind, message: str) -> ToolFailure:
    return ToolFailure(tool=_TOOL_NAME, kind=kind, message=message)


def _auth_headers(settings: Settings) -> dict[str, str]:
    """The Datadog auth headers — the only place the key values are read."""
    return {
        "DD-API-KEY": secret_value(settings.dd_api_key, "DD_API_KEY"),
        "DD-APPLICATION-KEY": secret_value(settings.dd_app_key, "DD_APP_KEY"),
        "Accept": "application/json",
    }


class _DatadogReader:
    """The Datadog reads of one fetch_logs call. Holds the headers for that call only."""

    def __init__(self, *, base_url: str, headers: dict[str, str], sleep: SleepFn) -> None:
        self._base_url = base_url
        self._headers = headers
        self._sleep = sleep

    def __repr__(self) -> str:  # never show the headers
        return f"_DatadogReader(base_url={self._base_url!r})"

    async def collect(
        self,
        client: httpx.AsyncClient,
        service: str,
        window: LogWindow,
        sources: list[LogSource],
    ) -> LogFetchResult:
        truncated = False
        synthetic_results: list[SyntheticResult] = []
        deploy_logs: list[DeployLogEntry] = []
        if "synthetics" in sources:
            synthetic_results, cut = await self._synthetics(client, service, window)
            truncated = truncated or cut
        if "deploys" in sources:
            deploy_logs, cut = await self._deploy_logs(client, service, window)
            truncated = truncated or cut
        return LogFetchResult(
            service=service,
            window=window,
            sources=sources,
            synthetic_results=synthetic_results,
            deploy_logs=deploy_logs,
            truncated=truncated,
        )

    # ── Synthetics ───────────────────────────────────────────────────────────

    async def _synthetics(
        self, client: httpx.AsyncClient, service: str, window: LogWindow
    ) -> tuple[list[SyntheticResult], bool]:
        """The service's API-test runs in the window, newest first, and whether any were cut."""
        listing = await self._request(client, "GET", "/api/v1/synthetics/tests")
        tag = f"service:{service}"
        tests = [
            test
            for test in _dicts(listing.get("tests"))
            if test.get("type") == "api"
            and isinstance(test.get("public_id"), str)
            and tag in (test.get("tags") or [])
        ]
        truncated = len(tests) > _MAX_TESTS
        tests = tests[:_MAX_TESTS]

        params = {"from_ts": _epoch_ms(window.start), "to_ts": _epoch_ms(window.end)}
        runs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for test in tests:
            path = f"/api/v1/synthetics/tests/{quote(test['public_id'], safe='')}/results"
            payload = await self._request(client, "GET", path, params=params)
            runs.extend((test, run) for run in _dicts(payload.get("results")))

        runs.sort(key=lambda pair: _number(pair[1].get("check_time")) or 0.0, reverse=True)
        truncated = truncated or len(runs) > _MAX_SYNTHETIC_RESULTS
        runs = runs[:_MAX_SYNTHETIC_RESULTS]

        results: list[SyntheticResult] = []
        details_left = _MAX_FAILURE_DETAILS
        for test, run in runs:
            result = _summary_result(test, run)
            result_id = run.get("result_id")
            if not result.passed and isinstance(result_id, str):
                if details_left > 0:
                    details_left -= 1
                    path = (
                        f"/api/v1/synthetics/tests/{quote(test['public_id'], safe='')}"
                        f"/results/{quote(result_id, safe='')}"
                    )
                    detail = await self._request(client, "GET", path)
                    result, cut = _with_detail(result, test, detail)
                    truncated = truncated or cut
                else:
                    truncated = True
            results.append(result)
        return results, truncated

    # ── CI pipeline logs ─────────────────────────────────────────────────────

    async def _deploy_logs(
        self, client: httpx.AsyncClient, service: str, window: LogWindow
    ) -> tuple[list[DeployLogEntry], bool]:
        """The service's deploy.completed logs in the window, newest first."""
        body = {
            "filter": {
                "query": f'service:{service} (@deploy.status:* OR "deploy.completed")',
                "from": _iso(window.start),
                "to": _iso(window.end),
            },
            "sort": "-timestamp",
            "page": {"limit": _MAX_DEPLOY_LOGS},
        }
        payload = await self._request(client, "POST", "/api/v2/logs/events/search", body=body)
        entries = [_deploy_entry(item) for item in _dicts(payload.get("data"))]
        meta = _obj(payload.get("meta"))
        page = _obj(meta.get("page"))
        # More pages, or the search timed out with partial results: not everything is here.
        truncated = bool(page.get("after")) or meta.get("status") == "timeout"
        return entries, truncated

    # ── HTTP ─────────────────────────────────────────────────────────────────

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one read with retries on transient errors; return the JSON object body.

        Raises:
            _DatadogFailure: a non-transient error status, a body that is not a JSON
                object, or a transient error on every attempt.
        """
        what = f"Datadog {method} {path}"
        reason = ""
        for attempt in range(1, _ATTEMPTS + 1):
            delay = _RETRY_DELAYS[attempt - 1]
            try:
                response = await client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=self._headers,
                    params=params,
                    json=body,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                reason = f"{type(exc).__name__}: {_cut(str(exc), _ERROR_TEXT_CHARS)[0]}"
            except httpx.HTTPError as exc:
                raise _DatadogFailure(f"{what} failed: {type(exc).__name__}") from exc
            else:
                status = response.status_code
                if status == 429 or status >= 500:
                    reason = f"HTTP {status}"
                    if status == 429:
                        delay = _retry_after(response, delay)
                elif not response.is_success:
                    raise _DatadogFailure(f"{what} returned HTTP {status}: {_error_text(response)}")
                else:
                    try:
                        data: object = response.json()
                    except ValueError as exc:
                        raise _DatadogFailure(f"{what} returned a non-JSON body") from exc
                    if not isinstance(data, dict):
                        raise _DatadogFailure(f"{what} returned an unexpected body")
                    return cast(dict[str, Any], data)
            logger.warning("datadog_request_retry", request=what, attempt=attempt, reason=reason)
            if attempt < _ATTEMPTS:
                await self._sleep(delay)
        raise _DatadogFailure(f"{what} failed after {_ATTEMPTS} attempts: {reason}")


# ── Parsing ───────────────────────────────────────────────────────────────────


def _summary_result(test: dict[str, Any], run: dict[str, Any]) -> SyntheticResult:
    """A run from the results summary: pass/fail, time, location, latency."""
    outcome = _obj(run.get("result"))
    timings = _obj(outcome.get("timings"))
    return SyntheticResult(
        test_name=str(test.get("name") or test["public_id"]),
        public_id=test["public_id"],
        checked_at=_check_time(run.get("check_time")),
        passed=outcome.get("passed") is True,
        location=_str(run.get("probe_dc")),
        latency_ms=_number(timings.get("total")),
    )


def _with_detail(
    result: SyntheticResult, test: dict[str, Any], detail: dict[str, Any]
) -> tuple[SyntheticResult, bool]:
    """Add a failed run's detail: status, failure, content type, body snippet, assertions."""
    outcome = _obj(detail.get("result"))
    failure = _obj(outcome.get("failure"))
    timings = _obj(outcome.get("timings"))

    message, message_cut = _cut(_str(failure.get("message")), _MESSAGE_CHARS)
    body, body_cut = _cut(_str(outcome.get("responseBody")), _BODY_SNIPPET_CHARS)
    status = outcome.get("httpStatusCode")

    # The assertions of the version that ran (detail.check), else the current test's.
    check = _obj(detail.get("check")) or test
    config = _obj(check.get("config"))
    assertions = [_describe_assertion(a) for a in _dicts(config.get("assertions"))]

    updated = result.model_copy(
        update={
            "status_code": status if isinstance(status, int) else None,
            "failure_code": _str(failure.get("code")),
            "failure_message": message,
            "content_type": _header(outcome.get("responseHeaders"), "content-type"),
            "body_snippet": body,
            "assertions": assertions[:_MAX_ASSERTIONS],
            "latency_ms": _number(timings.get("total")) or result.latency_ms,
        }
    )
    return updated, message_cut or body_cut or len(assertions) > _MAX_ASSERTIONS


def _describe_assertion(assertion: dict[str, Any]) -> str:
    """One assertion as text, e.g. "header content-type contains application/json"."""
    target: object = assertion.get("target")
    if isinstance(target, dict):  # validatesJSONPath and friends
        spec = cast(dict[str, Any], target)
        target = " ".join(
            str(spec[key])
            for key in ("jsonPath", "xPath", "operator", "targetValue")
            if key in spec
        )
    parts = [assertion.get("type"), assertion.get("property"), assertion.get("operator"), target]
    text = " ".join(str(part) for part in parts if part is not None and part != "")
    return _cut(text, 120)[0] or ""


def _deploy_entry(item: dict[str, Any]) -> DeployLogEntry:
    """A log event from the search → a compact deploy entry.

    The deploy fields come from the log's ``deploy`` attribute, falling back to its
    ``version`` / ``deploy_status`` / ``failed_stage`` tags.
    """
    attrs = _obj(item.get("attributes"))
    custom = _obj(attrs.get("attributes"))
    deploy = _obj(custom.get("deploy"))
    tags = _tags(attrs.get("tags"))
    pr_number = deploy.get("pr_number")
    return DeployLogEntry(
        timestamp=_parse_time(attrs.get("timestamp")),
        status=_str(attrs.get("status")),
        version=_str(deploy.get("version")) or tags.get("version"),
        deploy_status=_str(deploy.get("status")) or tags.get("deploy_status"),
        failed_stage=_str(deploy.get("failed_stage")) or tags.get("failed_stage"),
        pr_number=pr_number if isinstance(pr_number, int) and pr_number > 0 else None,
        message=_cut(_str(attrs.get("message")), _MESSAGE_CHARS)[0] or "",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _obj(value: object) -> dict[str, Any]:
    """A JSON object (anything else → empty)."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _dicts(value: object) -> list[dict[str, Any]]:
    """The object items of a JSON list (anything else → empty)."""
    items = cast(list[object], value) if isinstance(value, list) else []
    return [cast(dict[str, Any], item) for item in items if isinstance(item, dict)]


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _cut(text: str | None, limit: int) -> tuple[str | None, bool]:
    """*text* cut to *limit* characters, and whether it was cut."""
    if text is None or len(text) <= limit:
        return text, False
    return text[:limit], True


def _tags(value: object) -> dict[str, str]:
    """``["k:v", ...]`` → ``{k: v}`` (the first value of each key wins)."""
    tags: dict[str, str] = {}
    for tag in cast(list[object], value) if isinstance(value, list) else []:
        if isinstance(tag, str) and ":" in tag:
            key, _, val = tag.partition(":")
            tags.setdefault(key, val)
    return tags


def _header(headers: object, name: str) -> str | None:
    """A response header by case-insensitive name; list values are joined."""
    for key, value in _obj(headers).items():
        if key.lower() == name:
            if isinstance(value, list):
                value = ", ".join(str(v) for v in cast(list[object], value))
            return _cut(str(value), _MESSAGE_CHARS)[0]
    return None


def _check_time(value: object) -> datetime | None:
    """A Synthetics ``check_time`` (epoch; milliseconds, or seconds for small values)."""
    number = _number(value)
    if number is None:
        return None
    seconds = number / 1000 if number > 1e11 else number
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _iso(moment: datetime) -> str:
    """ISO-8601 UTC with a Z suffix, millisecond precision."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _retry_after(response: httpx.Response, default: float) -> float:
    """The wait a 429 asks for (Retry-After, else X-RateLimit-Reset), within bounds."""
    for header in ("Retry-After", "X-RateLimit-Reset"):
        raw = response.headers.get(header)
        if raw is None:
            continue
        try:
            wait = float(raw)
        except ValueError:
            continue
        return min(max(wait, 0.0), _MAX_RETRY_AFTER)
    return default


def _error_text(response: httpx.Response) -> str:
    """A short reason from an error response (Datadog's ``errors`` list when present)."""
    try:
        data: object = response.json()
    except ValueError:
        data = None
    errors = _obj(data).get("errors")
    if isinstance(errors, list):
        text = "; ".join(str(error) for error in cast(list[object], errors))
    else:
        text = response.text or response.reason_phrase
    return _cut(text, _ERROR_TEXT_CHARS)[0] or ""
