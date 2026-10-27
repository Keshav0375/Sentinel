"""scripts/ops/reconcile.py: backfill from recorded GitHub and LangFuse responses (task 10.3).

Every API response comes from tests/test_ops/fixtures (hand-shaped from the real APIs, including
fields the reconciler must ignore). FakeFetch serves them by (path, query) and fails on anything
unmapped; an autouse fixture makes urllib.request.urlopen raise, so no test reaches the network.
Ledger writes go to a local bare repo only.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts/ops"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

S = "/repos/Keshav0375/Sentinel"
D = "/repos/Keshav0375/Sentinel-deployment"
PAGE1 = {"per_page": "100", "page": "1"}
LF_BASE = "https://cloud.langfuse.com"
NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
T1 = "6f9619ff8b864011b42d00cf4fc964ff"
T2 = "1c2d3e4f5a6b4c7d8e9f0a1b2c3d4e5f"
CORR = "2a6c1f3e-9b7d-4e2a-8c5f-1d3b7e9a0c42"
INCIDENT = "0b6c3d2e-1f4a-4b5c-8d9e-0a1b2c3d4e5f"
APP_SOURCE = "gha:Keshav0375/Sentinel-deployment/ci_app_deployment.yml"
# Window filters: served from the same fixture, recorded in the URL.
IGNORED_PARAMS = ("created", "fromTimestamp")
# Every fixture run except 18800000102 (in progress).
COMPLETED_RUNS = {
    "18800000101",
    "18800000301",
    "18800000302",
    "18800000401",
    "18800000402",
    "18800000501",
    "18800000601",
    "18800000602",
    "18800000201",
    "18800000202",
    "18800000203",
}


def _route(path: str, **params: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    return path, tuple(sorted(params.items()))


def _runs(repo: str, workflow: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    return _route(f"{repo}/actions/workflows/{workflow}/runs", **PAGE1)


def _jobs(repo: str, run_id: int, attempt: int = 1) -> tuple[str, tuple[tuple[str, str], ...]]:
    return _route(f"{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", per_page="100")


ROUTES: dict[tuple[str, tuple[tuple[str, str], ...]], str] = {
    _runs(S, "ci_backend_deployment.yml"): "github/sentinel_runs_ci_backend_deployment.json",
    _runs(S, "ci_backend_scale.yml"): "github/sentinel_runs_ci_backend_scale.json",
    _runs(S, "ci.yml"): "github/sentinel_runs_ci.json",
    _runs(S, "ci_validation.yml"): "github/sentinel_runs_ci_validation.json",
    _runs(S, "infra_runners.yml"): "github/sentinel_runs_infra_runners.json",
    _runs(S, "infra_deploy.yml"): "github/sentinel_runs_infra_deploy.json",
    _runs(D, "ci_app_deployment.yml"): "github/deployment_runs_ci_app_deployment.json",
    _jobs(S, 18800000101): "github/sentinel_jobs_18800000101_1.json",
    _jobs(S, 18800000102): "github/sentinel_jobs_18800000102_1.json",
    _jobs(S, 18800000301): "github/sentinel_jobs_18800000301_1.json",
    _jobs(S, 18800000302): "github/sentinel_jobs_18800000302_1.json",
    _jobs(S, 18800000501): "github/sentinel_jobs_18800000501_1.json",
    _jobs(S, 18800000601): "github/sentinel_jobs_18800000601_1.json",
    _jobs(S, 18800000602): "github/sentinel_jobs_18800000602_1.json",
    _route(
        f"{S}/actions/runs/18800000401/attempts/1"
    ): "github/sentinel_attempt_18800000401_1.json",
    _jobs(D, 18800000201): "github/deployment_jobs_18800000201_1.json",
    _jobs(D, 18800000202): "github/deployment_jobs_18800000202_1.json",
    _jobs(D, 18800000203): "github/deployment_jobs_18800000203_1.json",
    _route(f"{D}/pulls/61"): "github/deployment_pull_61.json",
    _route(f"{D}/pulls/62"): "github/deployment_pull_62.json",
    _route(
        f"{D}/pulls", state="all", sort="updated", direction="desc", **PAGE1
    ): "github/deployment_pulls.json",
    _route(f"{D}/pulls/57/reviews", per_page="100"): "github/deployment_reviews_57.json",
    _route(f"{D}/pulls/56/reviews", per_page="100"): "github/deployment_reviews_56.json",
    _route(f"{D}/pulls/55/reviews", per_page="100"): "github/deployment_reviews_55.json",
    _route(
        "/api/public/traces", name="incident-pipeline", page="1", limit="100"
    ): "langfuse/traces_page1.json",
    _route(
        "/api/public/observations", traceId=T1, page="1", limit="100"
    ): f"langfuse/observations_{T1}.json",
    _route(
        "/api/public/observations", traceId=T2, page="1", limit="100"
    ): f"langfuse/observations_{T2}.json",
}


Route = tuple[str, tuple[tuple[str, str], ...]]
Answer = tuple[int, bytes, Mapping[str, str]]


class FakeFetch:
    """A Fetch serving fixtures by (path, query); records every request; fails on the unmapped.

    `responses` overrides a route with (status, headers) answers, served in order before the
    fixture; `bodies` replaces a route's fixture body."""

    def __init__(
        self,
        fail_hosts: tuple[str, ...] = (),
        responses: dict[Route, list[tuple[int, dict[str, str]]]] | None = None,
        bodies: dict[Route, Any] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.fail_hosts = fail_hosts
        self.responses = responses or {}
        self.bodies = bodies or {}

    def __call__(self, url: str, headers: Mapping[str, str]) -> Answer:
        self.calls.append(("GET", url, dict(headers)))
        parts = urllib.parse.urlsplit(url)
        if parts.hostname in self.fail_hosts:
            raise urllib.error.URLError("connection refused")
        params = {k: v for k, v in urllib.parse.parse_qsl(parts.query) if k not in IGNORED_PARAMS}
        key = (parts.path, tuple(sorted(params.items())))
        if self.responses.get(key):
            status, answer_headers = self.responses[key].pop(0)
            return status, b"{}", answer_headers
        if key in self.bodies:
            return 200, json.dumps(self.bodies[key]).encode(), {}
        if key not in ROUTES:
            raise AssertionError(f"unmapped request {key}")
        return 200, (FIXTURES / ROUTES[key]).read_bytes(), {}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network access in a test")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.fixture(scope="session")
def reconcile() -> ModuleType:
    if str(OPS_DIR) not in sys.path:
        sys.path.insert(0, str(OPS_DIR))
    spec = importlib.util.spec_from_file_location("ops_reconcile", OPS_DIR / "reconcile.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec: the dataclass resolves its postponed annotations here.
    sys.modules["ops_reconcile"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def no_sleep(reconcile: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(reconcile, "_sleep", slept.append)
    return slept


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _run(name: str, run_id: int) -> dict[str, Any]:
    runs = _fixture(f"github/{name}")["workflow_runs"]
    return next(r for r in runs if r["id"] == run_id)


def _gh(reconcile: ModuleType, fetch: FakeFetch | None = None) -> Any:
    return reconcile.github("test-gh-value", fetch or FakeFetch())


def _ids(specs: list[Any]) -> list[str]:
    return [s.event_id for s in specs]


def _collect(
    reconcile: ModuleType, *, langfuse: bool = True, fetch: FakeFetch | None = None
) -> Any:
    fetch = fetch or FakeFetch()
    lf = reconcile.langfuse(LF_BASE, "public-test", "secret-test", fetch) if langfuse else None
    return reconcile.collect(
        reconcile.github("test-gh-value", fetch),
        lf,
        github_since=None,
        langfuse_since=None,
        max_pages=reconcile.MAX_PAGES_FULL,
        now=NOW,
        lf_base=LF_BASE,
    )


# ── Sentinel workflows ────────────────────────────────────────────────────────


def test_backend_run_derivation_uses_shared_rules(reconcile: ModuleType) -> None:
    rules = reconcile.rules
    run = _run("sentinel_runs_ci_backend_deployment.json", 18800000101)

    specs = reconcile.backend_run_events(_gh(reconcile), run, 1)

    fact = rules.JobFact
    facts = {
        "preflight": fact("success", "2026-10-25T09:00:05Z", "2026-10-25T09:00:20Z"),
        "run-quality": fact("success", "2026-10-25T09:00:30Z", "2026-10-25T09:08:00Z"),
        "build-and-push": fact("success", "2026-10-25T09:08:10Z", "2026-10-25T09:15:00Z"),
        "deploy-to-aks": fact("success", "2026-10-25T09:15:10Z", "2026-10-25T09:25:00Z"),
        "run-tests": fact("success", "2026-10-25T09:25:10Z", "2026-10-25T09:38:00Z"),
        "promote-or-rollback": fact("success", "2026-10-25T09:38:10Z", "2026-10-25T09:40:30Z"),
    }
    expected = rules.backend_deploy_events(
        facts, sha=run["head_sha"], run_id=18800000101, run_attempt=1, with_started=True
    )
    assert specs == expected
    assert [s.kind for s in specs] == [
        "build.started",
        "build.succeeded",
        "deploy.started",
        "deploy.succeeded",
    ]
    started = next(s for s in specs if s.kind == "deploy.started")
    assert started.ts == "2026-10-25T09:15:10Z"
    assert {s.source_id for s in specs} == {"18800000101.1"}
    assert specs[-1].data["duration_s"] == 1370.0  # deploy start -> tests done


def test_in_progress_run_yields_started_only(reconcile: ModuleType) -> None:
    run = _run("sentinel_runs_ci_backend_deployment.json", 18800000102)

    specs = reconcile.backend_run_events(_gh(reconcile), run, 1)

    assert [s.kind for s in specs] == ["build.started"]
    assert specs[0].ts == "2026-10-25T11:50:30Z"


def test_scale_runs(reconcile: ModuleType) -> None:
    rules = reconcile.rules
    gh = _gh(reconcile)
    nightly = _run("sentinel_runs_ci_backend_scale.json", 18800000301)
    manual = _run("sentinel_runs_ci_backend_scale.json", 18800000302)

    night_specs = reconcile.scale_run_events(gh, nightly, 1)
    up_specs = reconcile.scale_run_events(gh, manual, 1)

    assert [s.data["replicas"] for s in night_specs] == [0]
    assert [s.data["replicas"] for s in up_specs] == [1]
    assert _ids(night_specs) == _ids(
        rules.scale_events(
            event_name="schedule",
            direction="down",
            result="success",
            keep_warm=None,
            run_id=18800000301,
            run_attempt=1,
        )
    )
    assert _ids(up_specs) == _ids(
        rules.scale_events(
            event_name="workflow_dispatch",
            direction="up",
            result="success",
            keep_warm=None,
            run_id=18800000302,
            run_attempt=1,
        )
    )
    assert up_specs[0].ts == "2026-10-25T07:40:55Z"


def test_scale_manual_down_is_event_time_only(reconcile: ModuleType) -> None:
    manual = _run("sentinel_runs_ci_backend_scale.json", 18800000302)
    jobs = _fixture("github/sentinel_jobs_18800000302_1.json")["jobs"]
    scale = next(j for j in jobs if j["name"] == "scale")
    for step in scale["steps"]:
        if step["name"].startswith("Bring"):
            step["conclusion"] = "skipped"
        if step["name"].startswith("Scale the backend down (manual"):
            step["conclusion"] = "success"

    def fetch(url: str, headers: Mapping[str, str]) -> Answer:
        return 200, json.dumps({"total_count": len(jobs), "jobs": jobs}).encode(), {}

    assert reconcile.scale_run_events(reconcile.github(None, fetch), manual, 1) == []


def test_pr_gate_events(reconcile: ModuleType) -> None:
    gh = _gh(reconcile)
    ok_run = _run("sentinel_runs_ci_validation.json", 18800000401)
    bad_run = _run("sentinel_runs_ci_validation.json", 18800000402)

    attempts = reconcile.run_attempts(gh, "Keshav0375/Sentinel", ok_run)
    specs = [reconcile.pr_gate_event(a, "ci_validation.yml") for a in attempts]
    startup = reconcile.pr_gate_event(bad_run, "ci_validation.yml")

    assert [s.source_id for s in specs] == ["18800000401.1", "18800000401.2"]
    assert [s.data["conclusion"] for s in specs] == ["failure", "success"]
    assert specs[1].data["duration_s"] == 720.0
    assert specs[1].ts == "2026-10-24T15:12:00Z"
    assert startup is not None and startup.data["conclusion"] == "failure"
    for spec in [*specs, startup]:
        assert spec.source == "gha:Keshav0375/Sentinel/ci_validation.yml"
        assert set(spec.chain) == {"sha", "run_id"}
    assert reconcile.pr_gate_event({**ok_run, "status": "in_progress"}, "ci.yml") is None
    assert reconcile.pr_gate_event({**ok_run, "conclusion": "mystery"}, "ci.yml") is None


def test_infra_runs(reconcile: ModuleType) -> None:
    rules = reconcile.rules
    gh = _gh(reconcile)
    runner = _run("sentinel_runs_infra_runners.json", 18800000501)
    apply = _run("sentinel_runs_infra_deploy.json", 18800000601)
    plan = _run("sentinel_runs_infra_deploy.json", 18800000602)

    runner_specs = reconcile.infra_runners_events(gh, runner, 1)
    apply_specs = reconcile.infra_deploy_run_events(gh, apply, 1)

    assert runner_specs == rules.infra_build_events(
        rules.JobFact("success", "2026-10-24T10:00:30Z", "2026-10-24T10:18:30Z"),
        sha=runner["head_sha"],
        run_id=18800000501,
        run_attempt=1,
        with_started=True,
    )
    assert [s.kind for s in runner_specs] == ["build.started", "build.succeeded"]
    assert apply_specs == rules.infra_deploy_events(
        rules.JobFact("success", "2026-10-24T11:00:30Z", "2026-10-24T11:26:00Z"),
        rules.JobFact("failure", "2026-10-24T11:26:10Z", "2026-10-24T11:38:00Z"),
        guards_passed=True,
        sha=apply["head_sha"],
        run_id=18800000601,
        run_attempt=1,
        with_started=True,
    )
    assert [s.kind for s in apply_specs] == ["deploy.started", "deploy.failed"]
    assert apply_specs[-1].data["failed_stage"] == "verify"
    assert apply_specs[-1].chain == {"run_id": 18800000601}
    assert reconcile.infra_deploy_run_events(gh, plan, 1) == []


# ── Sentinel-deployment ───────────────────────────────────────────────────────


def test_app_deploy_matrix(reconcile: ModuleType) -> None:
    specs = [s for s in _collect(reconcile, langfuse=False).specs if s.source == APP_SOURCE]
    by_run: dict[str, list[Any]] = {}
    for spec in specs:
        by_run.setdefault(spec.source_id, []).append(spec)

    assert {k: [s.kind for s in v] for k, v in by_run.items()} == {
        "18800000201.1": ["build.started", "build.succeeded", "deploy.started", "deploy.succeeded"],
        "18800000202.1": ["build.started", "build.succeeded", "deploy.started", "deploy.failed"],
        "18800000203.1": ["build.started", "build.failed"],
    }
    assert by_run["18800000202.1"][-1].data["failed_stage"] == "verify"
    assert by_run["18800000203.1"][-1].data["failed_stage"] == "build"
    assert by_run["18800000203.1"][-1].ts == "2026-10-25T10:08:00Z"  # Report build failure start
    versions = {k: {s.data["version"] for s in v} for k, v in by_run.items()}
    assert versions == {
        "18800000201.1": {"pr-61-4f1d2a9"},
        "18800000202.1": {"pr-62-7a3c9e1"},
        "18800000203.1": {"pr-0-3d2c1b0"},
    }
    assert {s.chain.get("scenario") for s in by_run["18800000201.1"]} == {"runtime/04"}
    assert all("scenario" not in s.chain for s in by_run["18800000202.1"])
    succeeded = by_run["18800000201.1"][-1]
    assert succeeded.chain["sha"] == "4f1d2a9c0b8e7d6f5a4b3c2d1e0f9a8b7c6d5e4f"
    assert succeeded.data["run_url"].endswith("/Sentinel-deployment/actions/runs/18800000201")
    assert reconcile.app_version("ab" * 20, "Merge pull request #62 from x/y\n\nbody") == (
        62,
        "pr-62-abababa",
    )
    assert reconcile.app_version("ab" * 20, "Fix x (#61)") == (61, "pr-61-abababa")
    assert reconcile.app_version("ab" * 20, None) == (0, "pr-0-abababa")


def test_revert_pr_events(reconcile: ModuleType) -> None:
    pulls = {p["number"]: p for p in _fixture("github/deployment_pulls.json")}

    def events(number: int) -> list[Any]:
        reviews = _fixture(f"github/deployment_reviews_{number}.json") if number != 61 else []
        return reconcile.revert_pr_events(pulls[number], reviews)

    open_pr, merged, closed = events(57), events(55), events(56)

    assert [s.kind for s in open_pr] == [
        "revert_pr.opened",
        "revert_pr.review_submitted",
        "revert_pr.review_submitted",
    ]
    assert [s.state for s in open_pr[1:]] == ["3100000001", "3100000002"]
    assert [s.data["review_state"] for s in open_pr[1:]] == ["COMMENTED", "APPROVED"]
    assert open_pr[0].data == {"url": pulls[57]["html_url"], "state": "open"}
    assert open_pr[1].data["waited_s"] == 1800.0
    assert open_pr[0].chain == {"pr": 57, "sha": "4f1d2a9", "incident_id": INCIDENT}
    assert {s.source for s in open_pr} == {"reconciler"}
    assert {s.source_id for s in open_pr} == {"Keshav0375/Sentinel-deployment#57"}

    assert [s.kind for s in merged] == [
        "revert_pr.opened",
        "revert_pr.review_submitted",
        "revert_pr.merged",
    ]
    assert merged[-1].data == {
        "url": pulls[55]["html_url"],
        "state": "merged",
        "reviewer": "Keshav0375",
        "waited_s": 2400.0,
    }
    assert merged[0].chain == {
        "pr": 55,
        "sha": "2c8f0e7",
        "incident_id": "a1b2c3d4-0000-4000-8000-000000000055",
    }
    assert [s.kind for s in closed] == ["revert_pr.opened", "revert_pr.closed"]
    assert closed[-1].data["waited_s"] == 3600.0
    assert events(61) == []
    for spec in [*open_pr, *merged, *closed]:
        assert "RAW-" not in json.dumps(spec.build())


def test_revert_pr_bad_login_is_dropped(reconcile: ModuleType) -> None:
    pull = _fixture("github/deployment_pulls.json")[2]  # 55, merged
    reviews = _fixture("github/deployment_reviews_55.json")
    reviews[0]["user"] = {"login": "not a login!"}

    specs = reconcile.revert_pr_events(pull, reviews)

    assert [s.kind for s in specs] == ["revert_pr.opened", "revert_pr.merged"]
    assert "reviewer" not in specs[-1].data


# ── LangFuse ──────────────────────────────────────────────────────────────────


def _trace(trace_id: str) -> dict[str, Any]:
    traces = _fixture("langfuse/traces_page1.json")["data"]
    return next(t for t in traces if t["id"] == trace_id)


def _observations(trace_id: str) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = _fixture(f"langfuse/observations_{trace_id}.json")["data"]
    return data


def test_trace_summary_is_names_and_timings_only(reconcile: ModuleType) -> None:
    spec = reconcile.trace_event(_trace(T1), _observations(T1), base_url=LF_BASE + "/", now=NOW)

    assert spec is not None
    event = spec.build()
    text = json.dumps(event)
    assert "RAW-" not in text  # input, output, statusMessage, metadata
    assert event["kind"] == "trace.summarized"
    assert event["source"] == "langfuse"
    assert event["ts"] == "2026-10-25T10:00:00Z"
    assert event["chain"] == {"incident_id": str(uuid.UUID(T1)), "correlation_id": CORR}
    data = event["data"]
    assert data["trace_id"] == T1
    assert data["langfuse_trace_url"] == f"{LF_BASE}/project/sentinel-dev/traces/{T1}"
    assert data["total_duration_ms"] == 74200
    spans = {s["span_id"]: s for s in data["spans"]}
    assert [s["span_id"] for s in data["spans"]] == [
        "obs-root",
        "obs-triage",
        "obs-investigate",
        "obs-tool-deploys",
        "obs-tool-logs",
        "obs-reason",
        "obs-report",
    ]
    assert "bad id!" not in text
    seen: set[str] = set()
    for span in data["spans"]:
        assert span["parent_id"] is None or span["parent_id"] in seen
        seen.add(span["span_id"])
    assert spans["obs-tool-logs"]["status"] == "error"
    assert spans["obs-tool-deploys"]["status"] == "warning"
    assert spans["obs-root"]["status"] == "ok"
    assert (spans["obs-triage"]["input_tokens"], spans["obs-triage"]["output_tokens"]) == (
        1200,
        180,
    )
    assert (spans["obs-reason"]["input_tokens"], spans["obs-reason"]["output_tokens"]) == (
        2400,
        410,
    )
    assert "input_tokens" not in spans["obs-root"]
    assert spans["obs-triage"] == {
        "span_id": "obs-triage",
        "parent_id": "obs-root",
        "name": "triage",
        "kind": "GENERATION",
        "start_ms": 1000,
        "duration_ms": 7250,
        "status": "ok",
        "input_tokens": 1200,
        "output_tokens": 180,
    }
    assert spans["obs-report"]["kind"] == "EVENT"


def test_unsettled_trace_skipped(reconcile: ModuleType) -> None:
    later = datetime(2026, 10, 26, tzinfo=UTC)
    young = datetime(2026, 10, 25, 10, 20, tzinfo=UTC)

    assert reconcile.trace_event(_trace(T2), _observations(T2), base_url=LF_BASE, now=later) is None
    assert reconcile.trace_event(_trace(T1), _observations(T1), base_url=LF_BASE, now=young) is None


def test_secret_like_span_name_skips_the_trace(
    reconcile: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    observations = _observations(T1)
    observations[0]["name"] = "Bearer abcdefghijklmnopqrstuvwxyz0123"
    lf_fetch = FakeFetch()

    def fetch(url: str, headers: Mapping[str, str]) -> Answer:
        if "traceId=" + T1 in url:
            body = json.dumps({"data": observations, "meta": {"totalPages": 1}}).encode()
            return 200, body, {}
        return lf_fetch(url, headers)

    out = reconcile.Outcome()
    reconcile._collect_langfuse(
        reconcile.langfuse(LF_BASE, "public-test", "secret-test", fetch),
        out,
        None,
        1,
        NOW,
        LF_BASE,
    )

    assert out.specs == []
    assert "trace.summarized" in capsys.readouterr().err


def test_every_derived_spec_builds(reconcile: ModuleType) -> None:
    outcome = _collect(reconcile)

    assert (outcome.github, outcome.langfuse) == ("ok", "ok")
    kinds = {s.kind for s in outcome.specs}
    assert {
        "ci.pr_gate.completed",
        "scale.changed",
        "revert_pr.merged",
        "trace.summarized",
    } <= kinds
    ids = _ids(outcome.specs)
    assert len(ids) == len(set(ids))
    for spec in outcome.specs:
        spec.build()


# ── HTTP ──────────────────────────────────────────────────────────────────────


def test_api_retries_then_fails_with_path_only(
    reconcile: ModuleType, no_sleep: list[float]
) -> None:
    answers: list[Answer] = [(502, b"", {}), (503, b"", {}), (200, b'{"ok": true}', {})]

    def flaky(url: str, headers: Mapping[str, str]) -> Answer:
        return answers.pop(0)

    api = reconcile.Api("https://api.github.com", {}, flaky)
    assert api.get("/x", {"q": "secret-query"}) == {"ok": True}
    assert no_sleep == [2, 4]

    def missing(url: str, headers: Mapping[str, str]) -> Answer:
        return 404, b"{}", {}

    with pytest.raises(reconcile.ApiError) as err:
        reconcile.Api("https://api.github.com", {"Authorization": "x"}, missing).get(
            "/repos/a/b", {"q": "secret-query"}
        )
    assert str(err.value) == "GET /repos/a/b answered HTTP 404"

    def down(url: str, headers: Mapping[str, str]) -> Answer:
        raise urllib.error.URLError("refused")

    with pytest.raises(reconcile.ApiError):
        reconcile.Api("https://api.github.com", {}, down).get("/y")
    assert no_sleep == [2, 4, 2, 4]


def test_only_get_requests(reconcile: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeFetch()
    seen: list[urllib.request.Request] = []

    class Response:
        def __init__(self, body: bytes) -> None:
            self.status = 200
            self.body = body
            self.headers: dict[str, str] = {}

        def read(self) -> bytes:
            return self.body

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def urlopen(request: urllib.request.Request, timeout: float) -> Response:
        assert timeout == 30
        seen.append(request)
        _, body, _ = fake(request.full_url, dict(request.header_items()))
        return Response(body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    outcome = reconcile.collect(
        reconcile.github("test-gh-value"),
        reconcile.langfuse(LF_BASE, "public-test", "secret-test"),
        github_since="2026-10-20T00:00:00Z",
        langfuse_since="2026-10-20T00:00:00Z",
        max_pages=reconcile.MAX_PAGES,
        now=NOW,
        lf_base=LF_BASE,
    )

    assert (outcome.github, outcome.langfuse) == ("ok", "ok")
    assert seen and {r.get_method() for r in seen} == {"GET"}
    for request in seen:
        assert "test-gh-value" not in request.full_url
        assert "public-test" not in request.full_url and "secret-test" not in request.full_url
        auth = request.get_header("Authorization")
        if request.full_url.startswith(LF_BASE):
            assert auth is not None and auth.startswith("Basic ")
            assert "fromTimestamp=2026-10-20" in request.full_url or "traceId=" in request.full_url
        else:
            assert auth == "Bearer test-gh-value"
            assert request.get_header("X-github-api-version") == "2022-11-28"
    runs = [r.full_url for r in seen if "/runs?" in r.full_url]
    assert runs and all("created=%3E%3D2026-10-20T00%3A00%3A00Z" in u for u in runs)


def test_no_network(reconcile: ModuleType) -> None:
    with pytest.raises(AssertionError, match="network"):
        reconcile.urllib_fetch("https://api.github.com/rate_limit", {})


# ── Cursor and appends ────────────────────────────────────────────────────────


def _cursor(since: str = "2026-10-25T06:00:00Z", swept: str = "2026-10-24T03:17:00Z") -> dict:
    return {
        "version": 1,
        "updated_at": since,
        "last_full_sweep_at": swept,
        "since": {"github": since, "langfuse": since},
    }


FLOOR = "2026-07-27T12:00:00Z"  # NOW - 90 days


def test_cursor_modes(reconcile: ModuleType, tmp_path: Path) -> None:
    """The weekly full sweep is date-triggered: over 7 days since the last one."""
    choose = reconcile.choose_mode

    assert reconcile.load_cursor(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text('{"version": 2}')
    assert reconcile.load_cursor(tmp_path / "bad.json") is None
    assert choose(None, full_flag="", now=NOW) == (True, FLOOR, FLOOR)
    fresh = _cursor()
    assert choose(fresh, full_flag="true", now=NOW)[0] is True
    assert choose(_cursor(swept="2026-10-19T12:00:00Z"), full_flag="", now=NOW)[0] is False
    assert choose(_cursor(swept="2026-10-18T12:00:00Z"), full_flag="", now=NOW)[0] is False
    assert choose(_cursor(swept="2026-10-18T11:59:59Z"), full_flag="", now=NOW)[0] is True
    assert choose(fresh, full_flag="false", now=NOW) == (
        False,
        "2026-10-25T00:00:00Z",
        "2026-10-25T00:00:00Z",
    )
    reconcile.save_cursor(tmp_path / "meta" / "cursor.json", fresh)
    assert reconcile.load_cursor(tmp_path / "meta" / "cursor.json") == {**fresh, "settled": {}}
    settled = {**fresh, "settled": {"18800000101": "2026-10-25T09:41:00Z"}}
    reconcile.save_cursor(tmp_path / "s.json", settled)
    assert reconcile.load_cursor(tmp_path / "s.json") == settled
    reconcile.save_cursor(tmp_path / "x.json", {**fresh, "settled": ["not", "a", "map"]})
    assert reconcile.load_cursor(tmp_path / "x.json") == {**fresh, "settled": {}}
    reconcile.save_cursor(tmp_path / "y.json", {**fresh, "last_full_sweep_attempt_at": "soon"})
    assert reconcile.load_cursor(tmp_path / "y.json") is None
    assert not hasattr(reconcile, "FULL_SWEEP_CRON")


def test_full_sweep_window_is_90_days(reconcile: ModuleType) -> None:
    assert reconcile.choose_mode(None, full_flag="", now=NOW) == (True, FLOOR, FLOOR)
    old = _cursor(since="2026-04-08T12:00:00Z", swept="2026-10-24T03:17:00Z")  # 200 days ago
    assert reconcile.choose_mode(old, full_flag="", now=NOW) == (False, FLOOR, FLOOR)
    unset = {**_cursor(), "since": {"github": None, "langfuse": None}}
    assert reconcile.choose_mode(unset, full_flag="", now=NOW) == (False, FLOOR, FLOOR)


def test_failed_sweep_retried_daily(reconcile: ModuleType) -> None:
    due = _cursor(swept="2026-10-17T12:00:00Z")  # 8 days old
    recent = {**due, "last_full_sweep_attempt_at": "2026-10-25T11:00:00Z"}  # 1 h ago
    day_old = {**due, "last_full_sweep_attempt_at": "2026-10-24T11:00:00Z"}  # 25 h ago

    assert reconcile.choose_mode(recent, full_flag="", now=NOW)[0] is False
    assert reconcile.choose_mode(day_old, full_flag="", now=NOW)[0] is True
    assert reconcile.choose_mode(recent, full_flag="true", now=NOW)[0] is True


def _ledger_with(tmp_path: Path, ids: list[str]) -> Path:
    day = tmp_path / "ledger" / "events" / "2026" / "10" / "25"
    day.mkdir(parents=True)
    for event_id in ids:
        (day / f"20261025T100000Z-build.started-{event_id}.json").write_text("{}")
    return tmp_path / "ledger"


def _main_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    for name in (
        "LANGFUSE_BASE_URL",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "OPS_FULL_SWEEP",
        "OPS_REVERT_PR_AUTHORS",
        "OPS_LEDGER_TOKEN",
        "OPS_LEDGER_REMOTE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "test-gh-value")
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


def test_cursor_not_advanced_on_error(
    reconcile: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_sleep: list[float],
) -> None:
    ledger_dir = _ledger_with(tmp_path, _ids(_collect(reconcile, langfuse=False).specs))
    cursor_path = tmp_path / "meta" / "cursor.json"
    reconcile.save_cursor(cursor_path, _cursor())
    status_path = tmp_path / "status.json"
    fetch = FakeFetch(fail_hosts=("cloud.langfuse.com",))
    monkeypatch.setattr(reconcile, "urllib_fetch", fetch)
    _main_env(
        monkeypatch,
        LANGFUSE_BASE_URL=LF_BASE,
        LANGFUSE_PUBLIC_KEY="public-test",
        LANGFUSE_SECRET_KEY="secret-test",
        OPS_LEDGER_REMOTE=(tmp_path / "no-remote.git").as_uri(),
    )

    rc = reconcile.main(
        [
            "run",
            "--ledger",
            str(ledger_dir),
            "--cursor",
            str(cursor_path),
            "--status",
            str(status_path),
            "--now",
            "2026-10-25T12:00:00Z",
        ]
    )

    assert rc == 0
    assert json.loads(status_path.read_text()) == {"github": "ok", "langfuse": "error"}
    saved = json.loads(cursor_path.read_text())
    assert saved["since"] == {"github": "2026-10-25T12:00:00Z", "langfuse": "2026-10-25T06:00:00Z"}
    assert saved["last_full_sweep_at"] == "2026-10-24T03:17:00Z"  # incremental run
    assert saved["last_full_sweep_attempt_at"] is None
    assert set(saved["settled"]) == COMPLETED_RUNS  # every append landed (nothing was missing)
    assert any("created=" in url for _, url, _ in fetch.calls)


def test_append_missing_skips_existing(reconcile: ModuleType, tmp_path: Path) -> None:
    specs = reconcile.backend_run_events(
        _gh(reconcile), _run("sentinel_runs_ci_backend_deployment.json", 18800000101), 1
    )
    have = reconcile.existing_ids(_ledger_with(tmp_path, [specs[0].event_id]))
    assert have == {specs[0].event_id}
    appended: list[str] = []

    def append(event: Mapping[str, Any], *, remote: str, token: str | None) -> Any:
        assert remote == "file:///fake" and token == "t"
        appended.append(event["event_id"])
        return reconcile.ledger.AppendResult(event["event_id"], "p", "written", 1)

    result = reconcile.append_missing(
        [*specs, specs[1]], have, token="t", remote="file:///fake", append=append
    )
    assert result == (3, 0, True)
    assert appended == _ids(specs[1:])

    appended.clear()
    assert reconcile.append_missing(
        specs, have, token="t", remote="file:///fake", append=append, limit=1
    ) == (1, 0, False)
    assert appended == [specs[1].event_id]

    def push_fails(event: Mapping[str, Any], *, remote: str, token: str | None) -> Any:
        raise reconcile.ledger.LedgerPushError("push failed")

    assert reconcile.append_missing(
        specs, have, token="t", remote="file:///fake", append=push_fails
    ) == (0, 0, False)
    assert reconcile.existing_ids(tmp_path / "absent") == set()


def test_main_end_to_end(
    reconcile: ModuleType,
    git_bin: str,
    tmp_path: Path,
    remote: Any,
    env_without_git: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(reconcile, "urllib_fetch", FakeFetch())
    _main_env(monkeypatch, OPS_LEDGER_REMOTE=remote.uri)
    cursor = tmp_path / "meta" / "cursor.json"
    status = tmp_path / "status.json"

    def run(ledger_dir: Path) -> str:
        argv = ["run", "--ledger", str(ledger_dir), "--cursor", str(cursor)]
        argv += ["--status", str(status), "--now", "2026-10-25T12:00:00Z"]
        assert reconcile.main(argv) == 0
        return capsys.readouterr().out

    first = run(tmp_path / "no-clone")
    derived = len(_collect(reconcile, langfuse=False).specs)
    assert f"mode=full derived={derived} written={derived} skipped=0" in first
    assert "github=ok langfuse=skipped" in first
    assert len(remote.events()) == derived
    assert json.loads(status.read_text()) == {"github": "ok", "langfuse": "skipped"}
    saved = json.loads(cursor.read_text())
    assert saved["last_full_sweep_at"] == "2026-10-25T12:00:00Z"
    assert saved["last_full_sweep_attempt_at"] == "2026-10-25T12:00:00Z"
    assert saved["since"] == {"github": "2026-10-25T12:00:00Z", "langfuse": None}
    assert set(saved["settled"]) == COMPLETED_RUNS

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "ops-ledger", remote.uri, str(clone)],
        env=env_without_git,
        check=True,
        capture_output=True,
    )
    fetch = FakeFetch()
    monkeypatch.setattr(reconcile, "urllib_fetch", fetch)
    second = run(clone)
    assert "mode=incremental" in second
    assert "written=0" in second
    # Settled runs are skipped: only the in-progress run's attempts are fetched again.
    fetched = {
        url.split("/actions/runs/")[1].split("/")[0]
        for _, url, _ in fetch.calls
        if "/attempts/" in url
    }
    assert fetched == {"18800000102"}
    assert len(remote.events()) == derived
    revert = [
        json.loads(remote.git("show", f"ops-ledger:{p}"))
        for p in remote.events()
        if "revert_pr" in p
    ]
    assert revert and {e["source"] for e in revert} == {"reconciler"}


def test_bad_arguments_exit_2(reconcile: ModuleType) -> None:
    with pytest.raises(SystemExit) as exc:
        reconcile.main(["run", "--ledger", "x"])
    assert exc.value.code == 2


# ── Rate limits ───────────────────────────────────────────────────────────────


def test_rate_limit_retry_after_honoured(reconcile: ModuleType, no_sleep: list[float]) -> None:
    answers: list[Answer] = [(429, b"", {"retry-after": "7"}), (200, b'{"ok": true}', {})]

    def fetch(url: str, headers: Mapping[str, str]) -> Answer:
        return answers.pop(0)

    assert reconcile.Api("https://api.github.com", {}, fetch).get("/x") == {"ok": True}
    assert no_sleep == [7]


def test_rate_limit_reset_bounded(
    reconcile: ModuleType, no_sleep: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000
    monkeypatch.setattr(reconcile, "_clock", lambda: float(now))

    def limited(reset: int) -> Any:
        answers: list[Answer] = [
            (403, b"", {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}),
            (200, b'{"ok": true}', {}),
        ]

        def fetch(url: str, headers: Mapping[str, str]) -> Answer:
            return answers.pop(0)

        return reconcile.Api("https://api.github.com", {}, fetch)

    assert limited(now + 10).get("/x") == {"ok": True}
    assert no_sleep == [11]
    no_sleep.clear()
    with pytest.raises(reconcile.ApiError, match="rate limited"):
        limited(now + 3600).get("/repos/a/b")
    assert no_sleep == []

    def forbidden(url: str, headers: Mapping[str, str]) -> Answer:
        return 403, b"{}", {"x-ratelimit-remaining": "4999"}

    with pytest.raises(reconcile.ApiError, match="HTTP 403"):
        reconcile.Api("https://api.github.com", {}, forbidden).get("/y")
    assert no_sleep == []


# ── Item isolation, settled runs, truncation ──────────────────────────────────


def _collect_with(reconcile: ModuleType, fetch: FakeFetch, **kwargs: Any) -> Any:
    return reconcile.collect(
        reconcile.github("test-gh-value", fetch),
        None,
        github_since=None,
        langfuse_since=None,
        max_pages=kwargs.pop("max_pages", reconcile.MAX_PAGES_FULL),
        now=NOW,
        lf_base=LF_BASE,
        **kwargs,
    )


def _source_ids(outcome: Any) -> set[str]:
    return {s.source_id for s in outcome.specs}


def test_settled_runs_skip_jobs(reconcile: ModuleType) -> None:
    baseline = _collect(reconcile, langfuse=False)
    fetch = FakeFetch()

    outcome = _collect_with(reconcile, fetch, settled={"18800000101": "2026-10-25T09:41:00Z"})

    assert not [u for _, u, _ in fetch.calls if "/runs/18800000101/attempts/" in u]
    assert "18800000101.1" not in _source_ids(outcome)
    others = [s for s in baseline.specs if s.source_id != "18800000101.1"]
    assert _ids(outcome.specs) == _ids(others)
    assert set(outcome.settled) == COMPLETED_RUNS - {"18800000101"}
    assert "18800000101" in outcome.run_ids
    # A changed updated_at (a re-run) is fetched again.
    rerun = _collect_with(reconcile, FakeFetch(), settled={"18800000101": "2026-10-01T00:00:00Z"})
    assert "18800000101.1" in _source_ids(rerun)


def test_bad_item_is_skipped(
    reconcile: ModuleType, no_sleep: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = _ids(_collect(reconcile, langfuse=False).specs)
    jobs_102 = _jobs(S, 18800000102)
    fetch = FakeFetch(responses={jobs_102: [(500, {}), (500, {}), (500, {})]})

    outcome = _collect_with(reconcile, fetch)

    assert (outcome.github, outcome.github_complete) == ("ok", False)
    assert "18800000102.1" not in _source_ids(outcome)
    assert "18800000101.1" in _source_ids(outcome)
    assert len(outcome.specs) == len(baseline) - 1  # 102 yields build.started only
    assert "run 18800000102 skipped" in capsys.readouterr().err

    runs = _fixture("github/sentinel_runs_ci_backend_deployment.json")
    for run in runs["workflow_runs"]:
        if run["id"] == 18800000101:
            del run["head_sha"]
    fetch = FakeFetch(bodies={_runs(S, "ci_backend_deployment.yml"): runs})

    outcome = _collect_with(reconcile, fetch)

    assert (outcome.github, outcome.github_complete) == ("ok", True)
    assert "18800000101.1" not in _source_ids(outcome)
    assert "18800000102.1" in _source_ids(outcome)
    assert "18800000101" not in outcome.settled
    assert "KeyError" in capsys.readouterr().err


def test_listing_failure_continues(reconcile: ModuleType) -> None:
    fetch = FakeFetch(responses={_runs(S, "ci.yml"): [(404, {})]})

    outcome = _collect_with(reconcile, fetch)

    assert outcome.github == "error"
    assert {"18800000301.1", "18800000302.1", "18800000501.1"} <= _source_ids(outcome)
    assert "18800000401.1" in _source_ids(outcome)  # ci_validation.yml, listed after ci.yml
    assert any(s.kind.startswith("revert_pr.") for s in outcome.specs)


def _hundred_runs() -> dict[str, Any]:
    run = _run("sentinel_runs_ci_validation.json", 18800000402)
    runs = [{**run, "id": 18800009000 + i} for i in range(100)]
    return {"total_count": 250, "workflow_runs": runs}


def test_truncation_holds_cursor(
    reconcile: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = {_runs(S, "ci.yml"): _hundred_runs()}

    outcome = _collect_with(reconcile, FakeFetch(bodies=bodies), max_pages=1)

    assert (outcome.github, outcome.github_complete) == ("ok", False)
    assert outcome.specs

    ledger_dir = _ledger_with(tmp_path, _ids(outcome.specs))
    cursor_path = tmp_path / "meta" / "cursor.json"
    reconcile.save_cursor(cursor_path, _cursor())
    monkeypatch.setattr(reconcile, "urllib_fetch", FakeFetch(bodies=bodies))
    monkeypatch.setattr(reconcile, "MAX_PAGES", 1)
    _main_env(monkeypatch, OPS_LEDGER_REMOTE=(tmp_path / "no-remote.git").as_uri())
    argv = ["run", "--ledger", str(ledger_dir), "--cursor", str(cursor_path)]
    argv += ["--status", str(tmp_path / "status.json"), "--now", "2026-10-25T12:00:00Z"]

    assert reconcile.main(argv) == 0

    saved = json.loads(cursor_path.read_text())
    assert saved["since"]["github"] == "2026-10-25T06:00:00Z"  # held
    assert saved["settled"]  # the runs that were read are still settled


# ── Revert-PR trust ───────────────────────────────────────────────────────────


def _pull(number: int) -> dict[str, Any]:
    return next(p for p in _fixture("github/deployment_pulls.json") if p["number"] == number)


def test_revert_pr_fork_rejected(reconcile: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    fork = _pull(57)
    fork["head"]["repo"]["full_name"] = "evil/Sentinel-deployment"
    reviews = _fixture("github/deployment_reviews_57.json")
    assert reconcile.revert_pr_events(fork, reviews) == []
    assert reconcile.revert_pr_accepted(fork, reconcile.DEFAULT_AUTHORS) == (
        "head is not Keshav0375/Sentinel-deployment"
    )
    deleted = _pull(57)
    deleted["head"]["repo"] = None
    assert reconcile.revert_pr_events(deleted, reviews) == []

    pulls = _fixture("github/deployment_pulls.json")
    pulls[0] = fork
    pulls_route = _route(f"{D}/pulls", state="all", sort="updated", direction="desc", **PAGE1)
    fetch = FakeFetch(bodies={pulls_route: pulls})

    outcome = _collect_with(reconcile, fetch)

    assert not [u for _, u, _ in fetch.calls if "/pulls/57/reviews" in u]
    assert "Keshav0375/Sentinel-deployment#57" not in _source_ids(outcome)
    assert "Keshav0375/Sentinel-deployment#55" in _source_ids(outcome)
    assert "PR #57 skipped: head is not" in capsys.readouterr().err


def test_revert_pr_wrong_author_rejected(
    reconcile: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    pull = _pull(57)
    pull["user"]["login"] = "mallory"
    reviews = _fixture("github/deployment_reviews_57.json")

    assert reconcile.revert_pr_events(pull, reviews) == []
    allowed = reconcile.parse_authors("mallory, Keshav0375")
    assert allowed == frozenset({"mallory", "keshav0375"})
    assert [s.kind for s in reconcile.revert_pr_events(pull, reviews, allowed)][0] == (
        "revert_pr.opened"
    )
    defaults = frozenset({"keshav0375", "github-actions[bot]"})
    assert reconcile.parse_authors("") == defaults
    assert reconcile.parse_authors(None) == defaults
    capsys.readouterr()
    assert reconcile.parse_authors("bad login!") == defaults
    assert "OPS_REVERT_PR_AUTHORS" in capsys.readouterr().err
    pull["user"]["login"] = "KESHAV0375"  # case-insensitive
    assert reconcile.revert_pr_events(pull, reviews) != []


def test_revert_pr_requires_marker(reconcile: ModuleType) -> None:
    pull = _pull(57)
    pull["body"] = "Reverts the deploy of 4f1d2a9.\n"
    reviews = _fixture("github/deployment_reviews_57.json")

    assert reconcile.revert_pr_events(pull, reviews) == []
    assert reconcile.revert_pr_accepted(pull, reconcile.DEFAULT_AUTHORS) == "no Sentinel marker"
    assert reconcile.revert_pr_accepted(_pull(61), reconcile.DEFAULT_AUTHORS) == (
        "not a revert branch"
    )


def test_only_trust_rejections_warn(
    reconcile: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """An ordinary PR (#61, branch runtime/04) is skipped silently; a rejected revert PR warns."""
    outcome = _collect_with(reconcile, FakeFetch())

    err = capsys.readouterr().err
    assert "#61" not in err and "not a revert branch" not in err
    assert "::warning::" not in err
    assert "Keshav0375/Sentinel-deployment#61" not in _source_ids(outcome)

    pulls = _fixture("github/deployment_pulls.json")
    pulls[0]["user"]["login"] = "mallory"  # #57
    pulls_route = _route(f"{D}/pulls", state="all", sort="updated", direction="desc", **PAGE1)
    _collect_with(reconcile, FakeFetch(bodies={pulls_route: pulls}))

    lines = [line for line in capsys.readouterr().err.splitlines() if "::warning::" in line]
    assert lines == ["::warning::ops-reconcile: PR #57 skipped: author not allowed"]
