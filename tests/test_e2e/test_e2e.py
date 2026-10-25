"""Offline end-to-end smoke: webhook → pipeline → rollback spec / escalation → eval judge.

The real app (``main.lifespan`` included) runs on the fake LLM against a throwaway Postgres
(TEST_DATABASE_URL: skipped locally without it, a failure in CI). The LangFuse dataset is a
recording fake, so the dataset item the pipeline writes is the one the eval reads back. The
autouse ``outbound`` guard (conftest) fails any request that leaves the process.
"""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from sentinel.config import Settings
from sentinel.eval.runner import DATASET_NAME, run_eval
from sentinel.memory.episodic import EpisodicMemory
from sentinel.models.eval_result import EvalDimension, GroundTruthCheck
from sentinel.models.tools import RollbackSpec
from sentinel.providers import PR_CONTENT_AGENT, fake_script, pr_content_turn
from test_e2e.conftest import (
    E2E,
    OTHER,
    SHA_A,
    SHA_B,
    SHA_C,
    Embedder,
    OutboundBlocked,
    OutboundGuard,
    catalog_path,
    insert_deploy,
    payload,
    post_and_finish,
    script_low,
    script_rollback,
)


async def _get(e2e: E2E, path: str) -> httpx.Response:
    return await e2e.client.get(path)


async def _linked(e2e: E2E, sha: str) -> uuid.UUID | None:
    return await e2e.pool.fetchval("SELECT incident_id FROM deployments WHERE commit_sha = $1", sha)


def _items(e2e: E2E) -> list[dict[str, Any]]:
    return [item.input for item in e2e.langfuse.items()]


async def test_path_a_deploy_failure_proposes_the_rollback(e2e: E2E) -> None:
    await insert_deploy(e2e.pool, 31, SHA_A, "failed")
    body = payload("deploy_failure", 31, SHA_A, "deployfail/03")
    with fake_script() as script:
        script_rollback(script, 31, SHA_A)
        incident_id = await post_and_finish(e2e, body)

        row = (await _get(e2e, f"/incidents/{incident_id}")).json()
        assert row["status"] == "rollback_proposed"
        assert row["correlation_id"] == body["correlation_id"]
        assert row["mttr_seconds"] is not None and row["mttr_seconds"] >= 0

        spec_response = await _get(e2e, f"/incidents/{incident_id}/rollback-spec")
        assert spec_response.status_code == 200
        target = spec_response.json()["target_deploy"]
        assert target["commit_sha"] == SHA_A and target["pr_number"] == 31

        assert await _linked(e2e, SHA_A) == uuid.UUID(incident_id)
        deploy = await _get(e2e, f"/incidents/{incident_id}/deploy")
        assert deploy.status_code == 200 and deploy.json()["commit_sha"] == SHA_A

        spec = RollbackSpec.model_validate(spec_response.json())
        script.on(PR_CONTENT_AGENT, pr_content_turn(spec))
        content = await e2e.client.post("/generate/pr-content", json=spec.model_dump(mode="json"))
        assert content.status_code == 200, content.text
        assert content.json()["title"] and content.json()["description"]

    (item,) = e2e.langfuse.items()
    assert item.id == incident_id
    assert item.input["branch"] == "deployfail/03"
    assert item.input["pr_number"] == 31 and item.input["merge_sha"] == SHA_A
    (record,) = item.input["incidents"]
    assert record["incident_id"] == incident_id
    assert record["signal_type"] == "deploy_failure"
    assert record["pipeline_result"]["status"] == "rollback_proposed"
    assert record["correlated_deploy"] == f"pr-31-{SHA_A}"


async def test_duplicate_correlation_id_is_202_duplicate(e2e: E2E) -> None:
    await insert_deploy(e2e.pool, 31, SHA_A, "failed")
    body = payload("deploy_failure", 31, SHA_A, "deployfail/03")
    with fake_script() as script:
        script_rollback(script, 31, SHA_A)
        incident_id = await post_and_finish(e2e, body)
        again = await e2e.client.post("/webhooks/incident", json=body)

    assert again.status_code == 202
    assert again.json()["status"] == "duplicate"
    assert again.json()["incident_id"] == incident_id
    count = await e2e.pool.fetchval(
        "SELECT count(*) FROM incidents WHERE correlation_id = $1",
        uuid.UUID(body["correlation_id"]),
    )
    assert count == 1
    assert len(e2e.langfuse.items()) == 1


async def test_path_b_runtime_error_escalates(e2e: E2E) -> None:
    await insert_deploy(e2e.pool, 32, SHA_B, "succeeded")
    with fake_script() as script:
        script_low(script, 32, SHA_B)
        incident_id = await post_and_finish(e2e, payload("runtime_error", 32, SHA_B, "runtime/01"))

        row = (await _get(e2e, f"/incidents/{incident_id}")).json()
        assert row["status"] == "escalated"
        assert (await _get(e2e, f"/incidents/{incident_id}/rollback-spec")).status_code == 404

    assert await _linked(e2e, SHA_B) is None
    (item,) = _items(e2e)
    assert item["branch"] == "runtime/01"
    (record,) = item["incidents"]
    assert record["pipeline_result"]["status"] == "escalated"
    assert record["correlated_deploy"] == f"pr-32-{SHA_B[:7]}"


async def test_path_b_runtime_error_can_propose_the_rollback(e2e: E2E) -> None:
    await insert_deploy(e2e.pool, 33, SHA_C, "succeeded")
    with fake_script() as script:
        script_rollback(script, 33, SHA_C)
        incident_id = await post_and_finish(e2e, payload("runtime_error", 33, SHA_C, "runtime/01"))

        row = (await _get(e2e, f"/incidents/{incident_id}")).json()
        assert row["status"] == "rollback_proposed"
        spec = (await _get(e2e, f"/incidents/{incident_id}/rollback-spec")).json()

    assert spec["target_deploy"]["commit_sha"] == SHA_C
    assert spec["target_deploy"]["pr_number"] == 33
    assert await _linked(e2e, SHA_C) == uuid.UUID(incident_id)


async def test_target_sha_mismatch_escalates(e2e: E2E) -> None:
    await insert_deploy(e2e.pool, 31, SHA_A, "failed")
    body = payload("deploy_failure", 31, OTHER, "deployfail/03")
    with fake_script() as script:
        script_rollback(script, 31, SHA_A)
        incident_id = await post_and_finish(e2e, body)

        row = (await _get(e2e, f"/incidents/{incident_id}")).json()
        assert row["status"] == "escalated"
        assert (await _get(e2e, f"/incidents/{incident_id}/rollback-spec")).status_code == 404

    assert await _linked(e2e, SHA_A) is None
    missing = row["trajectory"]["pipeline_result"]["escalation"]["missing"]
    assert "context.pr_details.sha" in missing[0]


async def test_judge_scores_the_written_items(
    e2e: E2E, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await insert_deploy(e2e.pool, 31, SHA_A, "failed")
    await insert_deploy(e2e.pool, 32, SHA_B, "succeeded")
    with fake_script() as script:
        script_rollback(script, 31, SHA_A)
        id_a = await post_and_finish(e2e, payload("deploy_failure", 31, SHA_A, "deployfail/03"))
    with fake_script() as script:
        script_low(script, 32, SHA_B)
        id_b = await post_and_finish(e2e, payload("runtime_error", 32, SHA_B, "runtime/01"))
    assert {item.id for item in e2e.langfuse.items()} == {id_a, id_b}

    runner_push, judge_push = MagicMock(), MagicMock()
    monkeypatch.setattr("sentinel.eval.runner.get_client", lambda: e2e.langfuse)
    monkeypatch.setattr("sentinel.eval.runner.langfuse_enabled", lambda: True)
    monkeypatch.setattr("sentinel.eval.runner.flush_traces", MagicMock())
    monkeypatch.setattr("sentinel.eval.runner.push_score", runner_push)
    monkeypatch.setattr("sentinel.eval.judge.push_score", judge_push)

    run = await run_eval(
        branches_path=catalog_path(),
        dataset_name=DATASET_NAME,
        settings=Settings(_env_file=None, sentinel_fake_llm=True),  # type: ignore[call-arg]
        reports_dir=tmp_path / "reports",
        run_name="e2e",
        score_store=EpisodicMemory(e2e.pool, Embedder()),  # type: ignore[arg-type]
    )

    assert run.invalid == {}
    assert len(run.results) == 2
    pushed_checks: dict[str, set[str]] = {}
    for call in runner_push.call_args_list:
        pushed_checks.setdefault(call.args[0], set()).add(call.args[1])
    for result in run.results:
        assert set(result.ground_truth.checks) == set(GroundTruthCheck)
        assert all(value == 1.0 for value in result.ground_truth.checks.values())
        judged = result.judge_score
        assert judged is not None
        assert not judged.judge_conflict and not judged.judge_failed
        assert len(judged.dimension_scores) == len(EvalDimension)
        assert all(0.0 <= ds.score <= 1.0 for ds in judged.dimension_scores)
        assert result.stored is True
        assert pushed_checks[f"trace-{result.item_id}"] == {str(c) for c in GroundTruthCheck}
    for incident_id in (id_a, id_b):
        stored = await e2e.pool.fetchval(
            "SELECT eval_score FROM incidents WHERE id = $1", uuid.UUID(incident_id)
        )
        assert stored is not None
        score = json.loads(stored) if isinstance(stored, str) else stored
        assert {"judge_model", "scores"} <= set(score)
    report = tmp_path / "reports" / "eval_report.json"
    assert report.is_file() and run.json_path == report


def test_outbound_guard_blocks_github(outbound: OutboundGuard) -> None:
    with pytest.raises(OutboundBlocked):
        httpx.get("https://api.github.com/")
    assert outbound.attempts == ["api.github.com"]
    outbound.attempts.clear()
    # A raw socket to an address that is neither loopback nor the test database is refused too.
    with socket.socket() as sock, pytest.raises(OutboundBlocked):
        sock.connect(("192.0.2.1", 443))
    assert outbound.attempts == ["192.0.2.1"]
    outbound.attempts.clear()  # the positive control: it must not fail the test at teardown
