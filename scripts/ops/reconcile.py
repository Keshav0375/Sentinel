#!/usr/bin/env python3
"""Backfill the ops ledger from the GitHub and LangFuse APIs (backend task 10.3; ops §4.2).

The workflows write their events at event time (task 10.2); this reconciler re-derives the same
facts from the read-only APIs, with the same kind/source/source_id/state recipe (rules.py), and
appends only the event_ids the ledger does not hold yet. A missed or failed event-time write is
therefore healed on the next run, and a fact already written is never duplicated (§3).

  source                                          API                       kinds
  Sentinel ci_backend_deployment.yml runs         /attempts/{n}/jobs        build.*, deploy.* (incl.
                                                                            deploy.started)
  Sentinel ci_backend_scale.yml runs              jobs (scale job steps)    scale.changed
  Sentinel ci.yml, ci_validation.yml runs         runs, /attempts/{n}       ci.pr_gate.completed
  Sentinel infra_runners.yml runs                 jobs                      build.*
  Sentinel infra_deploy.yml runs (apply only)     jobs, "Check guards"      deploy.*
  Sentinel-deployment ci_app_deployment.yml runs  jobs (step names)         build.*, deploy.*
  Sentinel-deployment revert-<sha> PRs            pulls, reviews            revert_pr.*
  LangFuse traces name=incident-pipeline          traces, observations      trace.summarized

Every attempt 1..run_attempt of a run is derived (source_id `<run_id>.<n>`). Incident kinds
(alert.received, agent.*, escalation.raised) are event-time only: the dispatch payload is not in
any API. Revert-PR events are written only here (source `reconciler`).

R14: a trace event carries names, timings and status only. Read are the trace's id, timestamp,
metadata.correlation_id and htmlPath, and the observation's id, parentObservationId, name, type,
startTime, endTime, level and usage (usageDetails) input/output. Never input, output,
statusMessage or any other metadata.

Revert-PR trust: a PR is a Sentinel revert PR only when its branch is `revert-<sha>`, its head repo
is Keshav0375/Sentinel-deployment (a fork or a deleted head repo is rejected), its body carries the
"Drafted by Sentinel for incident <id>." line ci_incident_response.yml writes, and its author is in
OPS_REVERT_PR_AUTHORS (comma-separated logins; default Keshav0375, github-actions[bot]).

Cursor (`--cursor`, persisted by actions/cache, never on ops-ledger):
  {"version": 1, "updated_at": ts, "last_full_sweep_at": ts|null,
   "last_full_sweep_attempt_at": ts|null, "since": {"github": ts|null, "langfuse": ts|null},
   "settled": {"<run_id>": "<run.updated_at>"}}
Every window starts no earlier than 90 days ago (GitHub's default Actions retention). Full sweep
when OPS_FULL_SWEEP is "true", when the cursor is missing or invalid, or when the last full sweep is
over 7 days old (weekly, date-triggered) unless a full sweep was attempted under a day ago, so a
failing sweep retries daily; else incremental from `since - OVERLAP_S`. A source's `since` advances
to this run's start only when that source fully succeeded, nothing was truncated or skipped on an
API error, and every append landed. A completed run whose events all landed is settled: while its
updated_at is unchanged, later runs skip its attempts and jobs.

One bad item (a run, a revert PR, a trace) is warned about and skipped, never the whole pull; a
failed listing marks the source "error" and the next listing still runs. A rate limit (429, or 403
with Retry-After or X-RateLimit-Remaining 0) is waited out when the wait is at most 60 s.

Read-only: GET requests only; the one write is ledger.append to ops-ledger. Always exits 0 once the
arguments parse (a pull failure is a status in `--status`, not a failed job).

CLI:
  reconcile.py run --ledger DIR --cursor PATH --status PATH [--now RFC3339]
  env GITHUB_TOKEN, OPS_LEDGER_TOKEN (default GITHUB_TOKEN), LANGFUSE_BASE_URL,
      LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, OPS_FULL_SWEEP, OPS_LEDGER_REMOTE,
      OPS_REVERT_PR_AUTHORS
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ops/: rules, ledger

import ledger  # noqa: E402
import rules  # noqa: E402

GITHUB_API = "https://api.github.com"
SENTINEL_RUN_WORKFLOWS = (
    "ci_backend_deployment.yml",
    "ci_backend_scale.yml",
    "ci.yml",
    "ci_validation.yml",
    "infra_runners.yml",
    "infra_deploy.yml",
)
PR_GATE_WORKFLOWS = ("ci.yml", "ci_validation.yml")
APP_WORKFLOW = "ci_app_deployment.yml"
APP_JOB = "Build Deploy and Verify"
SCALE_JOB = "scale"
INFRA_RUNNERS_JOB = "Build and Push Runner Image"
REVERT_BRANCH_RE = re.compile(r"^revert-([0-9a-f]{7,40})$")
INCIDENT_LINE_RE = re.compile(r"Drafted by Sentinel for incident ([0-9a-f-]{36})\.")
# Sentinel-deployment .github/scripts/deploy-metadata.sh: squash subject, then merge commit.
PR_SUBJECT_RES = (re.compile(r"\(#(\d+)\)\s*$"), re.compile(r"^Merge pull request #(\d+)"))
TRACE_NAME = "incident-pipeline"
TRACE_SETTLE_S = 1800
OVERLAP_S = 21600
MAX_PAGES = 5
MAX_PAGES_FULL = 30
PER_PAGE = 100
MAX_APPENDS_PER_RUN = 300
FULL_SWEEP_WINDOW_S = 90 * 86400  # GitHub's default Actions retention
FULL_SWEEP_MAX_AGE_S = 7 * 86400
FULL_SWEEP_RETRY_S = 86400
RATE_LIMIT_MAX_WAIT_S = 60
DEFAULT_REVERT_AUTHORS = ("Keshav0375", "github-actions[bot]")
DEFAULT_AUTHORS = frozenset(a.lower() for a in DEFAULT_REVERT_AUTHORS)
REVERT_REPO = rules.DEPLOYMENT
# event.v1.json data_pr_gate.conclusion.
PR_GATE_CONCLUSIONS = (
    "success",
    "failure",
    "cancelled",
    "skipped",
    "timed_out",
    "action_required",
    "neutral",
    "stale",
)
REVIEW_STATES = ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED")
HTTP_TIMEOUT_S = 30
RETRY_DELAYS_S = (2, 4)
SPAN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SPAN_KIND_RE = re.compile(r"^[A-Z_]{1,32}$")
TRACE_HEX_RE = re.compile(r"^[0-9a-f]{32}$")
# event.v1.json $defs: github_login, langfuse_trace_url.
LOGIN_RE = re.compile(r"^[A-Za-z0-9](-?[A-Za-z0-9]){0,38}(\[bot\])?$")
TRACE_URL_RE = re.compile(
    r"^https://[A-Za-z0-9.-]{1,100}/project/[A-Za-z0-9_-]{1,64}/traces/[A-Za-z0-9_-]{1,64}$"
)
MAX_SPANS = 500
MAX_MS = 86_400_000
MAX_TOKENS = 10_000_000
APP_SOURCE = rules.gha_source(rules.DEPLOYMENT, APP_WORKFLOW)
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_EVENT_FILE_RE = re.compile(r"-([0-9a-f]{64})\.json$")

_sleep = time.sleep  # looked up at call time so tests can monkeypatch reconcile._sleep
_clock = time.time  # likewise reconcile._clock

# (status, body, lower-cased response headers)
Fetch = Callable[[str, Mapping[str, str]], tuple[int, bytes, Mapping[str, str]]]
# Deterministic faults in one item's data: retrying cannot help, so the item is skipped.
DATA_ERRORS = (ValueError, KeyError, TypeError, AttributeError)


def warn(message: str) -> None:
    print(f"::warning::ops-reconcile: {message}", file=sys.stderr)


# ── HTTP ──────────────────────────────────────────────────────────────────────


class ApiError(Exception):
    """An API call failed (the message names the path only, never headers or query)."""


def _lower(headers: Any) -> dict[str, str]:
    items = headers.items() if headers is not None else []
    return {str(k).lower(): str(v) for k, v in items}


def urllib_fetch(url: str, headers: Mapping[str, str]) -> tuple[int, bytes, Mapping[str, str]]:
    """One GET; (status, body, headers). An HTTP error status is returned, a transport error
    raised."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            return int(response.status), response.read(), _lower(response.headers)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read() or b"", _lower(exc.headers)


def _rate_wait(status: int, headers: Mapping[str, str], attempt: int) -> float | None:
    """Seconds to wait out a rate limit; None when the answer is not a rate limit."""
    remaining = headers.get("x-ratelimit-remaining")
    if not (status == 429 or (status == 403 and ("retry-after" in headers or remaining == "0"))):
        return None
    try:
        return float(max(0, int(headers.get("retry-after", ""))))
    except ValueError:
        pass
    try:
        return max(0.0, int(headers.get("x-ratelimit-reset", "")) - _clock()) + 1
    except ValueError:
        return float(RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S) - 1)])


class Api:
    def __init__(self, base: str, headers: Mapping[str, str], fetch: Fetch = urllib_fetch) -> None:
        self.base = base.rstrip("/")
        self.headers = dict(headers)
        self.fetch = fetch

    def get(self, path: str, params: Mapping[str, str | int] | None = None) -> Any:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: str(v) for k, v in params.items()})
        failure = f"GET {path} failed"
        wait: float | None = None  # a rate-limit wait replaces the next retry delay
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            if attempt:
                _sleep(RETRY_DELAYS_S[attempt - 1] if wait is None else wait)
                wait = None
            try:
                status, body, headers = self.fetch(url, self.headers)
            except OSError:  # URLError, timeouts, resets
                failure = f"GET {path} failed: unreachable"
                continue
            wait = _rate_wait(status, headers, attempt)
            if wait is not None:
                failure = f"GET {path} rate limited"
                if wait > RATE_LIMIT_MAX_WAIT_S:
                    raise ApiError(failure)
                continue
            if status >= 500:
                failure = f"GET {path} answered HTTP {status}"
                continue
            if not 200 <= status < 300:
                raise ApiError(f"GET {path} answered HTTP {status}")
            return json.loads(body.decode("utf-8"))
        raise ApiError(failure)


def github(token: str | None, fetch: Fetch = urllib_fetch) -> Api:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return Api(GITHUB_API, headers, fetch)


def langfuse(base_url: str, public_key: str, secret_key: str, fetch: Fetch = urllib_fetch) -> Api:
    basic = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    headers = {"Authorization": f"Basic {basic}", "Accept": "application/json"}
    return Api(base_url, headers, fetch)


# ── Time helpers ──────────────────────────────────────────────────────────────


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _fmt(value: datetime) -> str:
    return value.astimezone(UTC).strftime(_TS_FORMAT)


def _seconds(start: Any, end: Any) -> float | None:
    return rules.seconds_between(start if isinstance(start, str) else None, end)


# ── GitHub listings ───────────────────────────────────────────────────────────


def list_runs(
    api: Api, repo: str, workflow_file: str, *, created_since: str | None, max_pages: int
) -> tuple[list[dict[str, Any]], bool]:
    """(runs, truncated): truncated when the last allowed page was full."""
    runs: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        params: dict[str, str | int] = {"per_page": PER_PAGE, "page": page}
        if created_since:
            params["created"] = f">={created_since}"
        body = api.get(f"/repos/{repo}/actions/workflows/{workflow_file}/runs", params)
        batch = [r for r in body.get("workflow_runs") or [] if isinstance(r, dict)]
        runs += batch
        if len(batch) < PER_PAGE:
            return runs, False
    return runs, True


def run_attempts(api: Api, repo: str, run: dict[str, Any]) -> list[dict[str, Any]]:
    """The attempt objects 1..run_attempt (the run itself is the latest)."""
    latest = int(run.get("run_attempt") or 1)
    attempts = [
        api.get(f"/repos/{repo}/actions/runs/{run['id']}/attempts/{n}") for n in range(1, latest)
    ]
    return [*attempts, run]


def attempt_jobs(api: Api, repo: str, run_id: int, attempt: int) -> list[dict[str, Any]]:
    body = api.get(
        f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", {"per_page": PER_PAGE}
    )
    return [j for j in body.get("jobs") or [] if isinstance(j, dict)]


def _fact(job: Mapping[str, Any]) -> rules.JobFact:
    result = str(job.get("conclusion") or "") if job.get("status") == "completed" else ""
    return rules.JobFact(
        result=result, started_at=job.get("started_at"), completed_at=job.get("completed_at")
    )


def job_facts(jobs: list[dict[str, Any]], names: Mapping[str, str]) -> dict[str, rules.JobFact]:
    """Job display name -> job key, keyed facts (result = conclusion once completed)."""
    return {names[job["name"]]: _fact(job) for job in jobs if job.get("name") in names}


def _job(jobs: Iterable[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((job for job in jobs if job.get("name") == name), None)


def _step(job: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    return next((s for s in job.get("steps") or [] if s.get("name") == name), None)


def _ran(step: Mapping[str, Any] | None) -> bool:
    return step is not None and step.get("conclusion") not in (None, "skipped")


# ── Sentinel derivations ──────────────────────────────────────────────────────


def backend_run_events(api: Api, run: dict[str, Any], attempt: int) -> list[rules.EventSpec]:
    jobs = attempt_jobs(api, rules.SENTINEL, int(run["id"]), attempt)
    return rules.backend_deploy_events(
        job_facts(jobs, rules.BACKEND_DEPLOY_JOBS),
        sha=str(run["head_sha"]),
        run_id=int(run["id"]),
        run_attempt=attempt,
        with_started=True,
    )


def scale_run_events(api: Api, run: dict[str, Any], attempt: int) -> list[rules.EventSpec]:
    job = _job(attempt_jobs(api, rules.SENTINEL, int(run["id"]), attempt), SCALE_JOB)
    if job is None:
        return []
    direction = ""
    for key, step_name in rules.SCALE_STEPS.items():
        step = _step(job, step_name)
        if step is not None and step.get("conclusion") == "success":
            direction = key.split("-")[0]
            break
    fact = _fact(job)
    return rules.scale_events(
        event_name=str(run.get("event") or ""),
        direction=direction,
        result=fact.result,
        # The jobs API does not show SENTINEL_KEEP_WARM: a manual down stays event-time only.
        keep_warm=None,
        run_id=int(run["id"]),
        run_attempt=attempt,
        ts=fact.completed_at,
    )


def infra_runners_events(api: Api, run: dict[str, Any], attempt: int) -> list[rules.EventSpec]:
    jobs = attempt_jobs(api, rules.SENTINEL, int(run["id"]), attempt)
    build = job_facts(jobs, rules.INFRA_RUNNERS_JOBS).get("build-and-push", rules.JobFact())
    return rules.infra_build_events(
        build,
        sha=str(run["head_sha"]),
        run_id=int(run["id"]),
        run_attempt=attempt,
        with_started=True,
    )


def infra_deploy_run_events(api: Api, run: dict[str, Any], attempt: int) -> list[rules.EventSpec]:
    jobs = attempt_jobs(api, rules.SENTINEL, int(run["id"]), attempt)
    run_job = next(
        (j for j in jobs if str(j.get("name", "")).startswith(rules.INFRA_DEPLOY_RUN_JOB_PREFIX)),
        None,
    )
    if run_job is None:  # plan or destroy
        return []
    verify = _job(jobs, rules.INFRA_DEPLOY_VERIFY_JOB)
    guards = _step(run_job, rules.INFRA_GUARDS_STEP)
    return rules.infra_deploy_events(
        _fact(run_job),
        _fact(verify) if verify is not None else rules.JobFact(),
        guards_passed=guards is not None and guards.get("conclusion") == "success",
        sha=str(run["head_sha"]),
        run_id=int(run["id"]),
        run_attempt=attempt,
        with_started=True,
    )


def pr_gate_event(run_attempt_obj: Mapping[str, Any], workflow_file: str) -> rules.EventSpec | None:
    """ci.pr_gate.completed for one completed attempt of ci.yml / ci_validation.yml (D4)."""
    if run_attempt_obj.get("status") != "completed":
        return None
    conclusion = str(run_attempt_obj.get("conclusion") or "")
    if conclusion == "startup_failure":
        conclusion = "failure"
    if conclusion not in PR_GATE_CONCLUSIONS:
        return None
    run_id = int(run_attempt_obj["id"])
    updated = run_attempt_obj.get("updated_at")
    return rules.EventSpec(
        kind="ci.pr_gate.completed",
        source=rules.gha_source(rules.SENTINEL, workflow_file),
        source_id=rules.run_source_id(run_id, int(run_attempt_obj.get("run_attempt") or 1)),
        chain={"sha": run_attempt_obj.get("head_sha"), "run_id": run_id},
        data={
            "conclusion": conclusion,
            "duration_s": _seconds(run_attempt_obj.get("run_started_at"), updated),
            "run_url": rules.run_url(rules.SENTINEL, run_id),
        },
        ts=updated if isinstance(updated, str) else None,
    )


# ── Sentinel-deployment derivations ───────────────────────────────────────────


def app_version(sha: str, head_commit_message: str | None) -> tuple[int, str]:
    """(PR number, `pr-<n>-<sha7>`), as deploy-metadata.sh derives APP_VERSION."""
    subject = (head_commit_message or "").replace("\r", "").split("\n", 1)[0]
    number = 0
    for pattern in PR_SUBJECT_RES:
        match = pattern.search(subject)
        if match:
            number = int(match.group(1))
            break
    return number, f"pr-{number}-{sha[:7]}"


def app_deploy_events(
    job: Mapping[str, Any],
    *,
    run_id: int,
    run_attempt: int,
    sha: str,
    version: str,
    scenario: str | None,
) -> list[rules.EventSpec]:
    """build.*/deploy.* for one Build Deploy and Verify job, from its step outcomes (D5)."""
    chain: dict[str, Any] = {"sha": sha, "run_id": run_id}
    if scenario:
        chain["scenario"] = scenario
    url = rules.run_url(rules.DEPLOYMENT, run_id)
    completed = job.get("status") == "completed"
    job_bad = completed and job.get("conclusion") in rules.BAD

    def event(kind: str, ts: Any, start: Any, stage: str | None = None) -> rules.EventSpec:
        data = {
            "version": version,
            "run_url": url,
            "duration_s": _seconds(start, ts),
            "failed_stage": stage,
        }
        return rules.EventSpec(
            kind=kind,
            source=APP_SOURCE,
            source_id=rules.run_source_id(run_id, run_attempt),
            chain=chain,
            data={k: v for k, v in data.items() if v is not None},
            ts=ts if isinstance(ts, str) else None,
        )

    events: list[rules.EventSpec] = []
    job_start = job.get("started_at")
    build = _step(job, "Build zip package")
    if build is not None and build.get("started_at") and build.get("conclusion") != "skipped":
        events.append(event("build.started", build["started_at"], None))
    report_build = _step(job, "Report build failure")
    if _ran(report_build):
        assert report_build is not None
        events.append(event("build.failed", report_build.get("started_at"), job_start, "build"))
        return events
    if build is None or build.get("conclusion") != "success":
        if job_bad and not _ran(build):
            events.append(event("build.failed", job.get("completed_at"), job_start, "build"))
        return events
    events.append(event("build.succeeded", build.get("completed_at"), job_start))

    deploy = _step(job, "Deploy to App Service")
    deploy_start = deploy.get("started_at") if deploy is not None else None
    if deploy is not None and deploy_start and deploy.get("conclusion") != "skipped":
        events.append(event("deploy.started", deploy_start, None))
    report_deploy = _step(job, "Report deploy failure")
    report_verify = _step(job, "Report verify failure")
    verify = _step(job, "Verify deployment")
    if _ran(report_deploy):
        assert report_deploy is not None
        events.append(
            event("deploy.failed", report_deploy.get("started_at"), deploy_start, "deploy")
        )
    elif _ran(report_verify):
        assert report_verify is not None
        events.append(
            event("deploy.failed", report_verify.get("started_at"), deploy_start, "verify")
        )
    elif verify is not None and verify.get("conclusion") == "success":
        events.append(event("deploy.succeeded", verify.get("completed_at"), deploy_start))
    elif completed:
        events.append(event("deploy.failed", job.get("completed_at"), deploy_start, "deploy"))
    return events


def _login(user: Any) -> str | None:
    login = user.get("login") if isinstance(user, Mapping) else None
    return login if isinstance(login, str) and LOGIN_RE.match(login) else None


def parse_authors(text: str | None) -> frozenset[str]:
    """The revert-PR author allow-list (lower-cased); the default when empty or all invalid."""
    authors: set[str] = set()
    for entry in (text or "").split(","):
        login = entry.strip()
        if not login:
            continue
        if LOGIN_RE.match(login):
            authors.add(login.lower())
        else:
            warn("OPS_REVERT_PR_AUTHORS entry dropped (not a GitHub login)")
    return frozenset(authors) or DEFAULT_AUTHORS


def revert_pr_accepted(pull: Mapping[str, Any], authors: frozenset[str]) -> str | None:
    """None when `pull` is a Sentinel revert PR, else why it is not."""
    head: Any = pull.get("head")
    head = head if isinstance(head, Mapping) else {}
    if not REVERT_BRANCH_RE.match(str(head.get("ref") or "")):
        return "not a revert branch"
    repo: Any = head.get("repo")
    if not isinstance(repo, Mapping) or repo.get("full_name") != REVERT_REPO:
        return f"head is not {REVERT_REPO}"
    if not INCIDENT_LINE_RE.search(str(pull.get("body") or "")):
        return "no Sentinel marker"
    login = _login(pull.get("user"))
    if login is None or login.lower() not in authors:
        return "author not allowed"
    return None


def revert_pr_events(
    pull: Mapping[str, Any],
    reviews: list[dict[str, Any]],
    authors: frozenset[str] = DEFAULT_AUTHORS,
) -> list[rules.EventSpec]:
    """revert_pr.* for one Sentinel revert PR (D6); [] for any other PR (revert_pr_accepted)."""
    if revert_pr_accepted(pull, authors) is not None:
        return []
    head: Any = pull.get("head")
    head = head if isinstance(head, Mapping) else {}
    match = REVERT_BRANCH_RE.match(str(head.get("ref") or ""))
    if match is None:
        return []
    number = int(pull["number"])
    chain: dict[str, Any] = {"pr": number, "sha": match.group(1)}
    incident = INCIDENT_LINE_RE.search(str(pull.get("body") or ""))
    if incident and rules.UUID_RE.match(incident.group(1)):
        chain["incident_id"] = incident.group(1)
    url = pull.get("html_url")
    created = pull.get("created_at")

    def event(kind: str, ts: Any, data: dict[str, Any], state: str = "") -> rules.EventSpec:
        return rules.EventSpec(
            kind=kind,
            source="reconciler",
            source_id=f"{rules.DEPLOYMENT}#{number}",
            state=state,
            chain=chain,
            data={"url": url, **{k: v for k, v in data.items() if v is not None}},
            ts=ts if isinstance(ts, str) else None,
        )

    events = [event("revert_pr.opened", created, {"state": "open"})]
    submitted = sorted(
        (r for r in reviews if r.get("submitted_at") and r.get("state") != "PENDING"),
        key=lambda r: str(r["submitted_at"]),
    )
    approver: str | None = None
    for review in submitted:
        login = _login(review.get("user"))
        state = review.get("state")
        if login is None or state not in REVIEW_STATES:
            continue
        if state == "APPROVED":
            approver = login
        events.append(
            event(
                "revert_pr.review_submitted",
                review["submitted_at"],
                {
                    "reviewer": login,
                    "review_state": state,
                    "waited_s": _seconds(created, review["submitted_at"]),
                },
                state=str(review["id"]),
            )
        )
    merged_at, closed_at = pull.get("merged_at"), pull.get("closed_at")
    if merged_at:
        events.append(
            event(
                "revert_pr.merged",
                merged_at,
                {"state": "merged", "reviewer": approver, "waited_s": _seconds(created, merged_at)},
            )
        )
    elif closed_at:
        events.append(
            event(
                "revert_pr.closed",
                closed_at,
                {"state": "closed", "waited_s": _seconds(created, closed_at)},
            )
        )
    return events


# ── LangFuse ──────────────────────────────────────────────────────────────────


def _tokens(observation: Mapping[str, Any], name: str) -> int | None:
    for container in ("usage", "usageDetails"):
        usage = observation.get(container)
        if isinstance(usage, Mapping):
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_TOKENS:
                return value
    return None


def _clamp_ms(value: timedelta) -> int:
    return min(max(int(value.total_seconds() * 1000), 0), MAX_MS)


def trace_event(
    trace: Mapping[str, Any],
    observations: list[dict[str, Any]],
    *,
    base_url: str,
    now: datetime,
) -> rules.EventSpec | None:
    """trace.summarized for one settled incident-pipeline trace: names, timings, status (D7)."""
    trace_id = trace.get("id")
    started = _parse(trace.get("timestamp"))
    if not isinstance(trace_id, str) or not SPAN_ID_RE.match(trace_id) or started is None:
        return None
    if (now - started).total_seconds() < TRACE_SETTLE_S:
        return None
    if any(not o.get("endTime") for o in observations):
        return None

    kept: dict[str, dict[str, Any]] = {}
    for obs in observations:
        obs_id, start, end = obs.get("id"), _parse(obs.get("startTime")), _parse(obs.get("endTime"))
        if not isinstance(obs_id, str) or not SPAN_ID_RE.match(obs_id) or start is None:
            continue
        if end is None:
            continue
        kept[obs_id] = {"obs": obs, "start": start, "end": end}
    for item in kept.values():
        parent = item["obs"].get("parentObservationId")
        item["parent"] = parent if isinstance(parent, str) and parent in kept else None

    children: dict[str | None, list[str]] = {}
    for obs_id, item in kept.items():
        children.setdefault(item["parent"], []).append(obs_id)
    for siblings in children.values():
        siblings.sort(key=lambda i: (kept[i]["start"], i))
    order: list[str] = []
    stack = list(reversed(children.get(None, [])))
    while stack:
        current = stack.pop()
        order.append(current)
        stack.extend(reversed(children.get(current, [])))

    data: dict[str, Any] = {"trace_id": trace_id, "spans": []}
    if kept:
        first = min(item["start"] for item in kept.values())
        last = max(item["end"] for item in kept.values())
        data["total_duration_ms"] = _clamp_ms(last - first)
        for obs_id in order[:MAX_SPANS]:
            item = kept[obs_id]
            obs = item["obs"]
            obs_type = obs.get("type")
            kind = (
                obs_type if isinstance(obs_type, str) and SPAN_KIND_RE.match(obs_type) else "SPAN"
            )
            name = obs.get("name")
            level = obs.get("level")
            span: dict[str, Any] = {
                "span_id": obs_id,
                "parent_id": item["parent"],
                "name": (name if isinstance(name, str) and name else kind)[:120],
                "kind": kind,
                "start_ms": _clamp_ms(item["start"] - first),
                "duration_ms": _clamp_ms(item["end"] - item["start"]),
                "status": {"ERROR": "error", "WARNING": "warning"}.get(str(level), "ok"),
            }
            for field_name, usage_name in (("input_tokens", "input"), ("output_tokens", "output")):
                tokens = _tokens(obs, usage_name)
                if tokens is not None:
                    span[field_name] = tokens
            data["spans"].append(span)
    html_path = trace.get("htmlPath")
    if isinstance(html_path, str):
        url = base_url.rstrip("/") + html_path
        if TRACE_URL_RE.match(url):
            data["langfuse_trace_url"] = url

    chain: dict[str, Any] = {}
    if TRACE_HEX_RE.match(trace_id):
        chain["incident_id"] = str(uuid.UUID(trace_id))
    metadata = trace.get("metadata")
    correlation = metadata.get("correlation_id") if isinstance(metadata, Mapping) else None
    if isinstance(correlation, str) and rules.UUID_RE.match(correlation):
        chain["correlation_id"] = correlation
    return rules.EventSpec(
        kind="trace.summarized",
        source="langfuse",
        source_id=trace_id,
        chain=chain,
        data=data,
        ts=_fmt(started),
    )


def _langfuse_pages(
    api: Api, path: str, params: dict[str, str | int], max_pages: int
) -> tuple[list[dict[str, Any]], bool]:
    """(items, truncated): truncated when pages remained after the last allowed one."""
    items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        body = api.get(path, {**params, "page": page, "limit": PER_PAGE})
        items += [d for d in body.get("data") or [] if isinstance(d, dict)]
        meta = body.get("meta") if isinstance(body.get("meta"), Mapping) else {}
        if page >= int(meta.get("totalPages") or 0):
            return items, False
    return items, True


# ── Cursor ────────────────────────────────────────────────────────────────────


def load_cursor(path: Path) -> dict[str, Any] | None:
    try:
        cursor = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cursor, dict) or cursor.get("version") != 1:
        return None
    since = cursor.get("since")
    if not isinstance(since, dict):
        return None
    for key in ("github", "langfuse"):
        if since.get(key) is not None and _parse(since.get(key)) is None:
            return None
    for key in ("last_full_sweep_at", "last_full_sweep_attempt_at"):
        if cursor.get(key) is not None and _parse(cursor.get(key)) is None:
            return None
    settled = cursor.get("settled")
    if not isinstance(settled, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in settled.items()
    ):
        cursor["settled"] = {}
    return cursor


def choose_mode(
    cursor: Mapping[str, Any] | None, *, full_flag: str, now: datetime
) -> tuple[bool, str | None, str | None]:
    """(full sweep?, GitHub window start, LangFuse window start); no window starts before
    FULL_SWEEP_WINDOW_S ago."""
    floor = now - timedelta(seconds=FULL_SWEEP_WINDOW_S)
    if full_flag.strip().lower() == "true" or not cursor:
        return True, _fmt(floor), _fmt(floor)
    last = _parse(cursor.get("last_full_sweep_at"))
    if last is None or (now - last).total_seconds() > FULL_SWEEP_MAX_AGE_S:
        attempt = _parse(cursor.get("last_full_sweep_attempt_at"))
        if attempt is None or (now - attempt).total_seconds() >= FULL_SWEEP_RETRY_S:
            return True, _fmt(floor), _fmt(floor)
    since = cursor.get("since") or {}

    def window(key: str) -> str:
        value = _parse(since.get(key))
        if value is None:
            return _fmt(floor)
        return _fmt(max(value - timedelta(seconds=OVERLAP_S), floor))

    return False, window("github"), window("langfuse")


def prune_settled(settled: Mapping[str, str], keep: Iterable[str], now: datetime) -> dict[str, str]:
    """Drop settled runs last updated before the window floor (they are never listed again)."""
    floor = now - timedelta(seconds=FULL_SWEEP_WINDOW_S)
    seen = set(keep)
    out: dict[str, str] = {}
    for run_id, updated in settled.items():
        at = _parse(updated)
        if run_id in seen or (at is not None and at >= floor):
            out[run_id] = updated
    return out


def save_cursor(path: Path, cursor: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cursor, indent=2) + "\n", encoding="utf-8")


# ── Collect and append ────────────────────────────────────────────────────────


def existing_ids(ledger_dir: Path) -> set[str]:
    events = ledger_dir / "events"
    if not events.is_dir():
        return set()
    found: set[str] = set()
    for path in events.rglob("*.json"):
        match = _EVENT_FILE_RE.search(path.name)
        if match:
            found.add(match.group(1))
    return found


@dataclass
class Outcome:
    specs: list[rules.EventSpec] = field(default_factory=list)
    github: str = "ok"
    langfuse: str = "skipped"
    # False when a listing was truncated or an item was skipped on an API error.
    github_complete: bool = True
    langfuse_complete: bool = True
    # Completed runs whose item succeeded (settled once their appends land), and every run seen.
    settled: dict[str, str] = field(default_factory=dict)
    run_ids: set[str] = field(default_factory=set)


def _sentinel_specs(
    api: Api, workflow: str, run: dict[str, Any], attempts: Callable[[], list[dict[str, Any]]]
) -> list[rules.EventSpec]:
    specs: list[rules.EventSpec] = []
    if workflow in PR_GATE_WORKFLOWS:
        for attempt in attempts():
            spec = pr_gate_event(attempt, workflow)
            if spec is not None:
                specs.append(spec)
        return specs
    derive = {
        rules.BACKEND_DEPLOY_WF: backend_run_events,
        rules.SCALE_WF: scale_run_events,
        rules.INFRA_RUNNERS_WF: infra_runners_events,
        rules.INFRA_DEPLOY_WF: infra_deploy_run_events,
    }[workflow]
    for n in range(1, int(run.get("run_attempt") or 1) + 1):
        specs += derive(api, run, n)
    return specs


def _reason(exc: Exception) -> str:
    return str(exc) if isinstance(exc, ApiError) else type(exc).__name__


def _runs_listing(
    gh: Api, out: Outcome, repo: str, workflow: str, since: str | None, max_pages: int
) -> list[dict[str, Any]]:
    """One workflow's runs; a failed listing marks github "error" and yields nothing."""
    try:
        runs, truncated = list_runs(gh, repo, workflow, created_since=since, max_pages=max_pages)
    except (ApiError, *DATA_ERRORS) as exc:
        warn(f"{workflow} runs not listed ({_reason(exc)})")
        out.github = "error"
        return []
    if truncated:
        out.github_complete = False
    return runs


def _run_item(
    out: Outcome,
    run: Mapping[str, Any],
    settled: Mapping[str, str],
    derive: Callable[[], list[rules.EventSpec]],
) -> None:
    """Derive one run (all attempts) in isolation; a settled, unchanged run is skipped."""
    run_id, updated = str(run.get("id")), run.get("updated_at")
    out.run_ids.add(run_id)
    if isinstance(updated, str) and settled.get(run_id) == updated:
        return
    try:
        specs = derive()
    except ApiError as exc:
        warn(f"run {run_id} skipped ({exc})")
        out.github_complete = False
        return
    except DATA_ERRORS as exc:
        warn(f"run {run_id} skipped ({type(exc).__name__})")
        return
    out.specs += specs
    if run.get("status") == "completed" and isinstance(updated, str):
        out.settled[run_id] = updated


def _collect_github(
    gh: Api,
    out: Outcome,
    since: str | None,
    max_pages: int,
    *,
    settled: Mapping[str, str],
    authors: frozenset[str],
) -> None:
    for workflow in SENTINEL_RUN_WORKFLOWS:
        for run in _runs_listing(gh, out, rules.SENTINEL, workflow, since, max_pages):
            _run_item(
                out,
                run,
                settled,
                lambda workflow=workflow, run=run: _sentinel_specs(
                    gh, workflow, run, lambda: run_attempts(gh, rules.SENTINEL, run)
                ),
            )

    scenarios: dict[int, str | None] = {}

    def scenario_of(number: int) -> str | None:
        if number <= 0:
            return None
        if number not in scenarios:
            pull = gh.get(f"/repos/{rules.DEPLOYMENT}/pulls/{number}")
            ref = str((pull.get("head") or {}).get("ref") or "")
            scenarios[number] = ref if rules.SCENARIO_RE.match(ref) else None
        return scenarios[number]

    def app_specs(run: Mapping[str, Any]) -> list[rules.EventSpec]:
        sha = str(run["head_sha"])
        commit: Any = run.get("head_commit")
        commit = commit if isinstance(commit, Mapping) else {}
        number, version = app_version(sha, commit.get("message"))
        scenario = scenario_of(number)
        specs: list[rules.EventSpec] = []
        for n in range(1, int(run.get("run_attempt") or 1) + 1):
            job = _job(attempt_jobs(gh, rules.DEPLOYMENT, int(run["id"]), n), APP_JOB)
            if job is not None:
                specs += app_deploy_events(
                    job,
                    run_id=int(run["id"]),
                    run_attempt=n,
                    sha=sha,
                    version=version,
                    scenario=scenario,
                )
        return specs

    for run in _runs_listing(gh, out, rules.DEPLOYMENT, APP_WORKFLOW, since, max_pages):
        _run_item(out, run, settled, lambda run=run: app_specs(run))

    for page in range(1, max_pages + 1):
        try:
            pulls = gh.get(
                f"/repos/{rules.DEPLOYMENT}/pulls",
                {
                    "state": "all",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": PER_PAGE,
                    "page": page,
                },
            )
            if not isinstance(pulls, list):
                raise TypeError("pulls")
        except (ApiError, *DATA_ERRORS) as exc:
            warn(f"pulls not listed ({_reason(exc)})")
            out.github = "error"
            return
        stop = len(pulls) < PER_PAGE
        for pull in pulls:
            if since and str(pull.get("updated_at") or "") < since:
                stop = True
                break
            if not REVERT_BRANCH_RE.match(str((pull.get("head") or {}).get("ref") or "")):
                continue
            number = pull.get("number")
            reason = revert_pr_accepted(pull, authors)
            if reason is not None:
                warn(f"PR #{number} skipped: {reason}")
                continue
            try:
                reviews = gh.get(
                    f"/repos/{rules.DEPLOYMENT}/pulls/{int(number)}/reviews",
                    {"per_page": PER_PAGE},
                )
                out.specs += revert_pr_events(
                    pull, [r for r in reviews if isinstance(r, dict)], authors
                )
            except ApiError as exc:
                warn(f"PR #{number} skipped ({exc})")
                out.github_complete = False
            except DATA_ERRORS as exc:
                warn(f"PR #{number} skipped ({type(exc).__name__})")
        if stop:
            return
    out.github_complete = False  # the last allowed page was full


def _collect_langfuse(
    lf: Api, out: Outcome, since: str | None, max_pages: int, now: datetime, base: str
) -> None:
    params: dict[str, str | int] = {"name": TRACE_NAME}
    if since:
        params["fromTimestamp"] = since
    try:
        traces, truncated = _langfuse_pages(lf, "/api/public/traces", params, max_pages)
    except (ApiError, *DATA_ERRORS) as exc:
        warn(f"traces not listed ({_reason(exc)})")
        out.langfuse = "error"
        return
    if truncated:
        out.langfuse_complete = False
    for trace in traces:
        if not isinstance(trace.get("id"), str):
            continue
        try:
            # Observation truncation is ignored: MAX_SPANS caps the summary anyway.
            observations, _ = _langfuse_pages(
                lf, "/api/public/observations", {"traceId": trace["id"]}, MAX_PAGES_FULL
            )
            spec = trace_event(trace, observations, base_url=base, now=now)
        except ApiError as exc:
            warn(f"trace {trace['id'][:12]} skipped ({exc})")
            out.langfuse_complete = False
            continue
        except DATA_ERRORS as exc:
            warn(f"trace {trace['id'][:12]} skipped ({type(exc).__name__})")
            continue
        if spec is None:
            continue
        try:
            spec.build()
        except ledger.LedgerError as exc:
            warn(f"trace.summarized {spec.event_id[:12]} skipped ({type(exc).__name__})")
            continue
        out.specs.append(spec)


def collect(
    gh: Api,
    lf: Api | None,
    *,
    github_since: str | None,
    langfuse_since: str | None,
    max_pages: int,
    now: datetime,
    lf_base: str = "",
    settled: Mapping[str, str] | None = None,
    revert_authors: frozenset[str] | None = None,
) -> Outcome:
    out = Outcome()
    try:
        _collect_github(
            gh,
            out,
            github_since,
            max_pages,
            settled=settled or {},
            authors=DEFAULT_AUTHORS if revert_authors is None else revert_authors,
        )
    except (ApiError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        warn(f"github pull failed ({exc if isinstance(exc, ApiError) else type(exc).__name__})")
        out.github = "error"
    if lf is not None:
        try:
            out.langfuse = "ok"  # a failed traces listing sets "error"
            _collect_langfuse(lf, out, langfuse_since, max_pages, now, lf_base)
        except (ApiError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            warn(
                f"langfuse pull failed ({exc if isinstance(exc, ApiError) else type(exc).__name__})"
            )
            out.langfuse = "error"
    return out


def append_missing(
    specs: Iterable[rules.EventSpec],
    have: set[str],
    *,
    token: str | None,
    remote: str = ledger.LEDGER_REMOTE,
    append: Callable[..., ledger.AppendResult] = ledger.append,
    limit: int = MAX_APPENDS_PER_RUN,
) -> tuple[int, int, bool]:
    """Append the specs whose event_id is not in *have*; (written, skipped, complete)."""
    written = skipped = attempted = 0
    seen = set(have)
    for spec in specs:
        event_id = spec.event_id
        if event_id in seen:
            continue
        if attempted >= limit:
            return written, skipped, False
        seen.add(event_id)
        try:
            event = spec.build()
        except ledger.LedgerError as exc:
            warn(f"{spec.kind} {event_id[:12]} skipped ({type(exc).__name__})")
            skipped += 1
            continue
        attempted += 1
        try:
            result = append(event, remote=remote, token=token)
        except ledger.LedgerPushError as exc:
            warn(f"{spec.kind} {event_id[:12]} not pushed ({type(exc).__name__})")
            return written, skipped, False
        except ledger.LedgerError as exc:
            warn(f"{spec.kind} {event_id[:12]} skipped ({type(exc).__name__})")
            skipped += 1
            continue
        if result.status == "written":
            written += 1
    return written, skipped, True


# ── CLI ───────────────────────────────────────────────────────────────────────


def _now(value: str | None) -> datetime:
    parsed = _parse(value) if value else None
    if value and parsed is None:
        raise SystemExit(2)
    return parsed or datetime.now(UTC).replace(microsecond=0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reconcile.py", description="Sentinel ops reconciler")
    commands = parser.add_subparsers(dest="command", required=True)
    run_cmd = commands.add_parser("run", help="backfill the ledger and write the status file")
    run_cmd.add_argument("--ledger", required=True, type=Path)
    run_cmd.add_argument("--cursor", required=True, type=Path)
    run_cmd.add_argument("--status", required=True, type=Path)
    run_cmd.add_argument("--now")
    args = parser.parse_args(argv)
    now = _now(args.now)

    env = os.environ
    token = env.get("GITHUB_TOKEN") or None
    status = {"github": "error", "langfuse": "skipped"}
    mode = "full"
    derived = written = skipped = 0
    try:
        cursor = load_cursor(args.cursor)
        full, gh_since, lf_since = choose_mode(
            cursor, full_flag=env.get("OPS_FULL_SWEEP") or "", now=now
        )
        old_settled: dict[str, str] = (cursor or {}).get("settled") or {}
        mode = "full" if full else "incremental"
        lf_base = env.get("LANGFUSE_BASE_URL") or ""
        lf_public = env.get("LANGFUSE_PUBLIC_KEY") or ""
        lf_secret = env.get("LANGFUSE_SECRET_KEY") or ""
        # urllib_fetch is looked up at call time so tests can monkeypatch reconcile.urllib_fetch.
        lf = (
            langfuse(lf_base, lf_public, lf_secret, urllib_fetch)
            if lf_base and lf_public and lf_secret
            else None
        )
        outcome = collect(
            github(token, urllib_fetch),
            lf,
            github_since=gh_since,
            langfuse_since=lf_since,
            max_pages=MAX_PAGES_FULL if full else MAX_PAGES,
            now=now,
            lf_base=lf_base,
            settled=old_settled,
            revert_authors=parse_authors(env.get("OPS_REVERT_PR_AUTHORS")),
        )
        status = {"github": outcome.github, "langfuse": outcome.langfuse}
        derived = len(outcome.specs)
        written, skipped, complete = append_missing(
            outcome.specs,
            existing_ids(args.ledger),
            token=env.get("OPS_LEDGER_TOKEN") or token,
            remote=env.get("OPS_LEDGER_REMOTE") or ledger.LEDGER_REMOTE,
        )
        old = cursor or {}
        old_since = old.get("since") or {}
        stamp = _fmt(now)
        github_ok = complete and outcome.github == "ok" and outcome.github_complete
        langfuse_ok = complete and outcome.langfuse == "ok" and outcome.langfuse_complete
        # A skipped (unconfigured) LangFuse does not hold the sweep back.
        swept_ok = github_ok and (langfuse_ok or outcome.langfuse == "skipped")
        settled = {**old_settled, **(outcome.settled if complete else {})}
        save_cursor(
            args.cursor,
            {
                "version": 1,
                "updated_at": stamp,
                "last_full_sweep_at": (
                    stamp if full and swept_ok else old.get("last_full_sweep_at")
                ),
                "last_full_sweep_attempt_at": (
                    stamp if full else old.get("last_full_sweep_attempt_at")
                ),
                "since": {
                    "github": stamp if github_ok else old_since.get("github"),
                    "langfuse": stamp if langfuse_ok else old_since.get("langfuse"),
                },
                "settled": prune_settled(settled, outcome.run_ids, now),
            },
        )
    except Exception as exc:  # noqa: BLE001 - a pull failure is a status, never a failed job
        warn(f"reconcile failed ({type(exc).__name__})")
    try:
        args.status.parent.mkdir(parents=True, exist_ok=True)
        args.status.write_text(json.dumps(status) + "\n", encoding="utf-8")
    except OSError as exc:
        warn(f"status file not written ({type(exc).__name__})")
    print(
        f"ops-reconcile: mode={mode} derived={derived} written={written} skipped={skipped} "
        f"github={status['github']} langfuse={status['langfuse']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
