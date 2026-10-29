#!/usr/bin/env python3
"""Shape the ops.v1 events of one Sentinel workflow run for GITHUB_OUTPUT (backend task 10.2).

The ops jobs of the Sentinel workflows run `python3 scripts/ops/emit.py <command> >>
"$GITHUB_OUTPUT"` and hand each slot to the ops-event action. The derivation is rules.py (shared
with the reconciler); this file only reads the runner environment and prints.

Per slot it prints `<slot>-kind`, `<slot>-source-id`, `<slot>-state`, `<slot>-chain` and
`<slot>-data` (compact single-line JSON). An absent event prints the five lines empty, and the
workflow skips that ops-event step on `<slot>-kind == ''`. Each event is built and validated here
first, so a value the ledger would reject empties the slot instead of failing the push.

Commands (slots) and the environment they read, besides GITHUB_REPOSITORY, GITHUB_RUN_ID and
GITHUB_RUN_ATTEMPT:
  backend-start (build)                 GITHUB_SHA
  backend-deploy (build, deploy)        GITHUB_SHA, QUALITY_RESULT, BUILD_RESULT, DEPLOY_RESULT,
                                        TESTS_RESULT
  incident-start (alert, agent)         GITHUB_EVENT_PATH
  incident-result (decision, escalation) GITHUB_EVENT_PATH, ENSURE_RESULT, PIPELINE_RESULT,
                                        RESOLUTION_TYPE, CONFIDENCE, ROOT_CAUSE, EVIDENCE,
                                        INCIDENT_ID, PR_DETAILS
  scale (scale)                         GITHUB_EVENT_NAME, DIRECTION, SCALE_RESULT, KEEP_WARM
  infra-runners-start (build)           GITHUB_SHA
  infra-runners (build)                 GITHUB_SHA, BUILD_RESULT
  infra-deploy-start (deploy)           GITHUB_SHA
  infra-deploy (deploy)                 GITHUB_SHA, RUN_RESULT, VERIFY_RESULT, GUARDS_PASSED

Non-blocking: always exits 0. Warnings are `::warning::` workflow commands on stderr (stdout is
GITHUB_OUTPUT) and name a field, never a value.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/: incident_dispatch
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ops/: rules, ledger

import incident_dispatch  # noqa: E402
import ledger  # noqa: E402
import rules  # noqa: E402

SLOTS: dict[str, tuple[str, ...]] = {
    "backend-start": ("build",),
    "backend-deploy": ("build", "deploy"),
    "incident-start": ("alert", "agent"),
    "incident-result": ("decision", "escalation"),
    "scale": ("scale",),
    "infra-runners-start": ("build",),
    "infra-runners": ("build",),
    "infra-deploy-start": ("deploy",),
    "infra-deploy": ("deploy",),
}
# Which slot an event kind fills, per command.
SLOT_OF_KIND: dict[str, dict[str, str]] = {
    "incident-start": {"alert.received": "alert", "agent.started": "agent"},
    "incident-result": {
        "agent.decided": "decision",
        "agent.failed": "decision",
        "escalation.raised": "escalation",
    },
}
FIELDS = ("kind", "source-id", "state", "chain", "data")


class EmitError(Exception):
    """The run's environment cannot be shaped into events (message names a field only)."""


def warn(message: str) -> None:
    print(f"::warning::ops-emit: {message}", file=sys.stderr)


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _int_env(name: str) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        raise EmitError(f"{name} is not an integer") from None


def correlation_uuid(raw: object) -> str | None:
    """The backend's correlation UUID for the dispatch (incident_dispatch), None when missing."""
    try:
        return incident_dispatch.correlation_uuid(raw)
    except incident_dispatch.MappingError:
        return None


def _client_payload() -> dict[str, Any]:
    try:
        return incident_dispatch.read_client_payload(os.environ.get("GITHUB_EVENT_PATH"))
    except incident_dispatch.MappingError:
        raise EmitError("the dispatch payload is unreadable (GITHUB_EVENT_PATH)") from None


def _correlation(payload: Mapping[str, Any]) -> str:
    value = correlation_uuid(payload.get("correlation_id"))
    if value is None:
        raise EmitError("client_payload.correlation_id is missing; no incident events")
    return value


def _str_or_none(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _events(command: str) -> list[rules.EventSpec]:
    env = os.environ
    repo = env.get("GITHUB_REPOSITORY", "")
    if repo != rules.SENTINEL:
        raise EmitError(f"GITHUB_REPOSITORY is not {rules.SENTINEL}")
    run_id = _int_env("GITHUB_RUN_ID")
    run_attempt = _int_env("GITHUB_RUN_ATTEMPT")
    sha = env.get("GITHUB_SHA", "")
    run = {"sha": sha, "run_id": run_id, "run_attempt": run_attempt}

    if command == "backend-start":
        return [rules.backend_build_started(**run)]
    if command == "backend-deploy":
        jobs = {
            "run-quality": rules.JobFact(env.get("QUALITY_RESULT", "")),
            "build-and-push": rules.JobFact(env.get("BUILD_RESULT", "")),
            "deploy-to-aks": rules.JobFact(env.get("DEPLOY_RESULT", "")),
            "run-tests": rules.JobFact(env.get("TESTS_RESULT", "")),
        }
        return rules.backend_deploy_events(jobs, **run)
    if command == "incident-start":
        payload = _client_payload()
        event = payload.get("event")
        event = event if isinstance(event, dict) else {}
        tags = incident_dispatch.normalise_tags(event.get("tags"))
        return rules.incident_start_events(
            correlation_id=_correlation(payload),
            signal_type=_str_or_none(payload.get("signal_type")),
            service=incident_dispatch.service_of(tags) or None,
            severity=_str_or_none(event.get("severity")),
            run_id=run_id,
        )
    if command == "incident-result":
        payload = _client_payload()
        pr_details = incident_dispatch.load_json_env("PR_DETAILS")
        return rules.incident_result_events(
            correlation_id=_correlation(payload),
            ensure_result=env.get("ENSURE_RESULT", ""),
            pipeline_result=env.get("PIPELINE_RESULT", ""),
            resolution_type=env.get("RESOLUTION_TYPE", ""),
            confidence=env.get("CONFIDENCE", ""),
            root_cause=env.get("ROOT_CAUSE", "").strip(),
            evidence=env.get("EVIDENCE", "").strip(),
            incident_id=env.get("INCIDENT_ID", "").strip(),
            pr_details=pr_details if isinstance(pr_details, dict) else None,
            run_id=run_id,
            warn=warn,
        )
    if command == "scale":
        return rules.scale_events(
            event_name=env.get("GITHUB_EVENT_NAME", ""),
            direction=env.get("DIRECTION", ""),
            result=env.get("SCALE_RESULT", ""),
            keep_warm=rules.parse_bool(env.get("KEEP_WARM")),
            run_id=run_id,
            run_attempt=run_attempt,
        )
    if command == "infra-runners-start":
        return [rules.infra_build_started(**run)]
    if command == "infra-runners":
        return rules.infra_build_events(rules.JobFact(env.get("BUILD_RESULT", "")), **run)
    if command == "infra-deploy-start":
        return [rules.infra_deploy_started(**run)]
    return rules.infra_deploy_events(
        rules.JobFact(env.get("RUN_RESULT", "")),
        rules.JobFact(env.get("VERIFY_RESULT", "")),
        guards_passed=rules.parse_bool(env.get("GUARDS_PASSED")) or False,
        **run,
    )


def _slot_of(command: str, spec: rules.EventSpec) -> str:
    by_kind = SLOT_OF_KIND.get(command)
    if by_kind is not None:
        return by_kind[spec.kind]
    return spec.kind.split(".", 1)[0]  # build.* -> build, deploy.* -> deploy, scale.* -> scale


def print_slots(slots: Mapping[str, rules.EventSpec | None]) -> None:
    """Five GITHUB_OUTPUT lines per slot; a missing or unbuildable event prints them empty."""
    for slot, spec in slots.items():
        values = dict.fromkeys(FIELDS, "")
        if spec is not None:
            try:
                spec.build(warn=lambda message, slot=slot: warn(f"{slot}: {message}"))
            except ledger.LedgerError as exc:
                warn(f"{slot} event not built ({type(exc).__name__})")
            else:
                values = {
                    "kind": spec.kind,
                    "source-id": spec.source_id,
                    "state": spec.state,
                    "chain": _compact(spec.chain),
                    "data": _compact(spec.data),
                }
        for name in FIELDS:
            print(f"{slot}-{name}={values[name]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="emit.py", description="Shape ops.v1 events")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in SLOTS:
        commands.add_parser(command)
    args = parser.parse_args(argv)
    slots: dict[str, rules.EventSpec | None] = dict.fromkeys(SLOTS[args.command])
    try:
        events = _events(args.command)
    except EmitError as exc:
        warn(str(exc))
        events = []
    except Exception as exc:  # non-blocking helper: an unexpected fault empties every slot
        warn(f"no events ({type(exc).__name__})")
        events = []
    for spec in events:
        slot = _slot_of(args.command, spec)
        if slot in slots:
            slots[slot] = spec
    print_slots(slots)
    return 0


if __name__ == "__main__":
    sys.exit(main())
