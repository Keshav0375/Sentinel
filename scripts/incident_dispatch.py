#!/usr/bin/env python3
"""Shape data for the incident response workflow (.github/workflows/ci_incident_response.yml).

Pure functions plus a small CLI, stdlib only, so the workflow's YAML stays free of logic:

* ``tags``       service and PR number from the dispatch's Datadog tags
* ``payload``    the ``POST /webhooks/incident`` body from the dispatch and the fetched context
* ``pr-details`` ``context.pr_details`` (number, merge sha, author, files, branch = head.ref) from a
                 GitHub pull request and its file list
* ``logs``       ``context.recent_logs`` from a Datadog log search response
* ``result``     the workflow outputs from the polled incident (no flag means the poll timed out)
* ``summary``    the final resolution, outcome, severity, a record warning and the Datadog
                 event body

Values printed for GITHUB_OUTPUT are single-line. Nothing here reads a secret or calls a network.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from typing import Any

SIGNAL_TYPES = ("deploy_failure", "runtime_error")
TAG_KEYS = ("service", "deploy_status", "failed_stage", "version")
SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
VERSION_PR_RE = re.compile(r"^pr-([0-9]{1,9})$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
MAX_LOGS = 50
MAX_LOG_MESSAGE_CHARS = 500
MAX_FILES = 100
MAX_OUTPUT_CHARS = 500
TRUNCATED = "…[truncated]"
TIMEOUT_EVIDENCE = "pipeline timeout"
TERMINAL_STATUSES = ("rollback_proposed", "escalated")


class MappingError(ValueError):
    """The dispatch cannot be mapped to an incident payload."""


def correlation_uuid(raw: object) -> str:
    """A UUID for the dispatch's correlation id: itself when it is one, else a stable uuid5."""
    if not isinstance(raw, str) or not raw.strip():
        raise MappingError("client_payload.correlation_id is missing")
    value = raw.strip()
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "sentinel:" + value))


def normalise_tags(raw: object) -> dict[str, str]:
    """Datadog tags (``"k:v,k2:v2"``, a list of such items, or a dict) as a dict of TAG_KEYS."""
    items: list[str]
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, list):
        items = [item for item in raw if isinstance(item, str)]
    elif isinstance(raw, dict):
        return {
            key: value.strip()
            for key in TAG_KEYS
            if isinstance(value := raw.get(key), str) and value.strip()
        }
    else:
        return {}
    tags: dict[str, str] = {}
    for item in items:
        key, sep, val = item.strip().partition(":")
        key, val = key.strip(), val.strip()
        if sep and val and key in TAG_KEYS:
            tags[key] = val
    return tags


def service_of(tags: dict[str, str]) -> str:
    """The ``service`` tag when it is safe to use in a query, else an empty string."""
    service = tags.get("service", "")
    return service if SERVICE_RE.match(service) else ""


def pr_number_of(tags: dict[str, str]) -> str:
    """The PR number of a ``version:pr-<N>`` tag, else an empty string."""
    match = VERSION_PR_RE.match(tags.get("version", ""))
    return match.group(1) if match else ""


def single_line(value: object, limit: int = MAX_OUTPUT_CHARS) -> str:
    """*value* on one line, cut to *limit* characters."""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if len(text) > limit:
        return text[:limit] + TRUNCATED
    return text


def build_payload(
    client_payload: dict[str, Any],
    *,
    service_metadata: Any = None,
    pr_details: Any = None,
    recent_logs: Any = None,
) -> dict[str, Any]:
    """The ``POST /webhooks/incident`` body for a ``repository_dispatch`` client payload."""
    signal_type = client_payload.get("signal_type")
    if signal_type not in SIGNAL_TYPES:
        raise MappingError(f"client_payload.signal_type must be one of {', '.join(SIGNAL_TYPES)}")
    event = client_payload.get("event")
    if not isinstance(event, dict):
        raise MappingError("client_payload.event is not an object")
    title = event.get("title")
    if not isinstance(title, str) or not title.strip():
        raise MappingError("client_payload.event.title is missing")
    context: dict[str, Any] = {}
    if isinstance(service_metadata, dict):
        context["service_metadata"] = service_metadata
    if isinstance(pr_details, dict):
        context["pr_details"] = pr_details
    if isinstance(recent_logs, list):
        context["recent_logs"] = recent_logs
    payload: dict[str, Any] = {
        "signal_type": signal_type,
        "correlation_id": correlation_uuid(client_payload.get("correlation_id")),
        "title": title.strip(),
        "source": "datadog",
        "tags": normalise_tags(event.get("tags")),
        "context": context,
    }
    for key in ("alert_id", "dd_event_id", "severity"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            payload[key] = value.strip()
    return payload


def shape_pr_details(pull: dict[str, Any], files: list[str]) -> dict[str, Any] | None:
    """``context.pr_details`` from a pull request; None unless it is merged with a merge sha."""
    sha = pull.get("merge_commit_sha")
    if pull.get("merged") is not True or not isinstance(sha, str) or not SHA40_RE.match(sha):
        return None
    user = pull.get("user")
    head = pull.get("head")
    ref = head.get("ref") if isinstance(head, dict) else None
    return {
        "number": int(pull["number"]),
        "sha": sha,
        "author": user.get("login") if isinstance(user, dict) else None,
        "files_changed": [name for name in files if name][:MAX_FILES],
        "branch": ref.strip() if isinstance(ref, str) and ref.strip() else None,
    }


def shape_logs(response: dict[str, Any]) -> list[dict[str, str]]:
    """``context.recent_logs`` from a Datadog logs search response."""
    data = response.get("data")
    if not isinstance(data, list):
        return []
    logs: list[dict[str, str]] = []
    for item in data[:MAX_LOGS]:
        attributes = item.get("attributes") if isinstance(item, dict) else None
        attrs: dict[str, Any] = attributes if isinstance(attributes, dict) else {}
        entry = {
            key: value if isinstance(value := attrs.get(key), str) else ""
            for key in ("timestamp", "status", "service", "message")
        }
        if len(entry["message"]) > MAX_LOG_MESSAGE_CHARS:
            entry["message"] = entry["message"][:MAX_LOG_MESSAGE_CHARS] + TRUNCATED
        logs.append(entry)
    return logs


def _escalated(evidence: str) -> dict[str, str]:
    return {
        "resolution_type": "escalated",
        "root_cause": "",
        "confidence": "0",
        "target_deploy": "",
        "evidence_summary": evidence,
        "missing": "",
    }


def result_outputs(incident: dict[str, Any] | None) -> dict[str, str]:
    """The run-agent-pipeline outputs from the polled incident; None means the poll timed out."""
    if incident is None or incident.get("status") not in TERMINAL_STATUSES:
        return _escalated(TIMEOUT_EVIDENCE)
    trajectory = incident.get("trajectory")
    result = trajectory.get("pipeline_result") if isinstance(trajectory, dict) else None
    if not isinstance(result, dict):
        return _escalated("pipeline result missing")
    rollback = incident.get("status") == "rollback_proposed"
    rollback = rollback and result.get("resolution_type") == "rollback"
    try:
        confidence = f"{float(result.get('confidence')):.2f}"
    except (TypeError, ValueError):
        confidence = "0"
    target = result.get("target_deploy")
    escalation = result.get("escalation")
    missing = ""
    if isinstance(escalation, dict) and isinstance(escalation.get("missing"), list):
        missing = single_line("; ".join(str(item) for item in escalation["missing"]))
    return {
        "resolution_type": "rollback" if rollback else "escalated",
        "root_cause": single_line(result.get("root_cause")),
        "confidence": confidence,
        "target_deploy": (
            target if rollback and isinstance(target, str) and SHA_RE.match(target) else ""
        ),
        "evidence_summary": single_line(result.get("evidence_summary")),
        "missing": missing,
    }


def summary_outputs(
    *,
    ensure_result: str,
    pipeline_result: str,
    create_result: str,
    record_result: str,
    resolution_type: str,
    pr_url: str,
    evidence: str,
    service: str,
    incident_id: str,
    run_url: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    """The final resolution, outcome, severity and record warning, and the Datadog event body."""
    warning = ""
    if ensure_result != "success":
        resolution, outcome, severity = (
            "failed",
            "incident NOT processed: the backend did not come up",
            "critical",
        )
    elif pipeline_result != "success":
        resolution, outcome, severity = (
            "failed",
            f"incident NOT processed: the agent pipeline job ended {pipeline_result}",
            "critical",
        )
    elif resolution_type == "rollback" and pr_url.strip():
        resolution, outcome, severity = (
            "rollback",
            f"revert PR opened for review: {pr_url}",
            "warning",
        )
        if record_result != "success":
            warning = (
                f"the revert PR was not recorded on the incident ({record_result or 'skipped'})"
            )
            outcome += f"; {warning}"
    elif resolution_type == "rollback":
        resolution, outcome, severity = (
            "failed",
            f"rollback drafted but the revert PR was not opened ({create_result or 'skipped'})",
            "critical",
        )
    else:
        resolution, severity = "escalated", "warning"
        outcome = (
            "escalated: pipeline timeout"
            if evidence == TIMEOUT_EVIDENCE
            else "escalated: needs human review"
        )
    outcome = single_line(outcome)
    tags = ["source:sentinel", f"resolution:{resolution}"]
    if service:
        tags.append(f"service:{service}")
    event = {
        "title": f"Sentinel incident {resolution}",
        "text": f"{outcome}\nincident: {incident_id or 'none'}\nrun: {run_url}",
        "tags": tags,
        "alert_type": {"info": "info", "warning": "warning", "critical": "error"}[severity],
    }
    outputs = {
        "resolution": resolution,
        "outcome": outcome,
        "severity": severity,
        "warning": single_line(warning),
    }
    return outputs, event


def read_client_payload(path: str | None) -> dict[str, Any]:
    """``client_payload`` of the ``repository_dispatch`` event file at *path*."""
    if not path or not os.path.isfile(path):
        raise MappingError("the event file is missing (GITHUB_EVENT_PATH)")
    with open(path, encoding="utf-8") as handle:
        try:
            event = json.load(handle)
        except json.JSONDecodeError as exc:
            raise MappingError(f"the event file is not JSON: {exc.msg}") from exc
    client_payload = event.get("client_payload") if isinstance(event, dict) else None
    if not isinstance(client_payload, dict):
        raise MappingError("client_payload is not an object")
    return client_payload


def load_json_env(name: str) -> Any:
    """JSON from environment variable *name*; None when unset, blank or invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"::warning::incident_dispatch: {name} is not valid JSON; ignored", file=sys.stderr)
        return None


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _print_outputs(outputs: dict[str, str]) -> None:
    for key, value in outputs.items():
        print(f"{key}={single_line(value)}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tags")
    sub.add_parser("payload")
    pr = sub.add_parser("pr-details")
    pr.add_argument("--pull", required=True)
    pr.add_argument("--files", required=True)
    logs = sub.add_parser("logs")
    logs.add_argument("--response", required=True)
    result = sub.add_parser("result")
    result.add_argument("--incident")
    summary = sub.add_parser("summary")
    summary.add_argument("--dd-event-file", required=True)
    return parser


def _run(args: argparse.Namespace) -> int:
    env = os.environ
    if args.command == "tags":
        event = read_client_payload(env.get("GITHUB_EVENT_PATH")).get("event")
        tags = normalise_tags(event.get("tags") if isinstance(event, dict) else None)
        print(f"service={service_of(tags)}")
        print(f"pr_number={pr_number_of(tags)}")
    elif args.command == "payload":
        payload = build_payload(
            read_client_payload(env.get("GITHUB_EVENT_PATH")),
            service_metadata=load_json_env("SERVICE_METADATA"),
            pr_details=load_json_env("PR_DETAILS"),
            recent_logs=load_json_env("RECENT_LOGS"),
        )
        print(_compact(payload))
    elif args.command == "pr-details":
        pull = _read_json(args.pull)
        with open(args.files, encoding="utf-8") as handle:
            files = handle.read().splitlines()
        shaped = shape_pr_details(pull, files) if isinstance(pull, dict) else None
        print(_compact(shaped))
    elif args.command == "logs":
        response = _read_json(args.response)
        print(_compact(shape_logs(response if isinstance(response, dict) else {})))
    elif args.command == "result":
        incident = _read_json(args.incident) if args.incident else None
        _print_outputs(result_outputs(incident if isinstance(incident, dict) else None))
    else:
        outputs, event = summary_outputs(
            ensure_result=env.get("ENSURE_RESULT", ""),
            pipeline_result=env.get("PIPELINE_RESULT", ""),
            create_result=env.get("CREATE_RESULT", ""),
            record_result=env.get("RECORD_RESULT", ""),
            resolution_type=env.get("RESOLUTION_TYPE", ""),
            pr_url=env.get("PR_URL", ""),
            evidence=env.get("EVIDENCE", ""),
            service=env.get("SERVICE", ""),
            incident_id=env.get("INCIDENT_ID", ""),
            run_url=env.get("RUN_URL", ""),
        )
        with open(args.dd_event_file, "w", encoding="utf-8") as handle:
            handle.write(_compact(event))
        _print_outputs(outputs)
        if outputs["warning"]:
            print(f"::warning::incident_dispatch: {outputs['warning']}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand; a MappingError is a workflow error annotation and exit 1."""
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except MappingError as exc:
        print(f"::error::incident_dispatch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
