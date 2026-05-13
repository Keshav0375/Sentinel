"""Tests for sentinel.agents.loader — prompt file loading and content validation."""

from __future__ import annotations

import pytest

from sentinel.agents.loader import load_prompt

# All six agent prompt files that must exist.
PROMPT_FILES = [
    "orchestrator.txt",
    "triage.txt",
    "log_analyst.txt",
    "deploy_correlator.txt",
    "remediation.txt",
    "comms.txt",
]

# ── Existence and basic structure ─────────────────────────────────────────────


@pytest.mark.parametrize("filename", PROMPT_FILES)
def test_prompt_loads(filename: str) -> None:
    """Every prompt file must load without error and return a non-empty string."""
    text = load_prompt(filename)
    assert isinstance(text, str)
    assert len(text) > 0


@pytest.mark.parametrize("filename", PROMPT_FILES)
def test_prompt_starts_with_role_declaration(filename: str) -> None:
    """Every prompt must begin with 'You are the ...' on the first line."""
    text = load_prompt(filename)
    first_line = text.splitlines()[0]
    assert first_line.startswith("You are the "), (
        f"{filename}: first line must start with 'You are the ...', got: {first_line!r}"
    )


@pytest.mark.parametrize("filename", PROMPT_FILES)
def test_prompt_under_token_budget(filename: str) -> None:
    """Each prompt must stay under ~800 tokens (estimated as words / 0.75)."""
    text = load_prompt(filename)
    word_count = len(text.split())
    estimated_tokens = word_count / 0.75
    assert estimated_tokens <= 800, (
        f"{filename}: estimated {estimated_tokens:.0f} tokens exceeds 800-token budget"
    )


# ── Caching ───────────────────────────────────────────────────────────────────


def test_load_prompt_is_cached() -> None:
    """load_prompt returns the same object on repeated calls (lru_cache)."""
    a = load_prompt("orchestrator.txt")
    b = load_prompt("orchestrator.txt")
    assert a is b


# ── Missing file error ────────────────────────────────────────────────────────


def test_load_prompt_missing_file_raises() -> None:
    """load_prompt raises FileNotFoundError for a non-existent file."""
    with pytest.raises(FileNotFoundError, match="does_not_exist.txt"):
        load_prompt("does_not_exist.txt")


# ── Content constraints per agent ─────────────────────────────────────────────


def test_orchestrator_mentions_tool_call_cap() -> None:
    """Orchestrator prompt must enforce the 15 tool-call cap."""
    text = load_prompt("orchestrator.txt")
    assert "15" in text, "orchestrator.txt must reference the 15 tool-call cap"
    assert "escalat" in text.lower(), "orchestrator.txt must mention escalation on cap exceeded"


def test_orchestrator_mentions_all_specialists() -> None:
    """Orchestrator prompt must reference all five specialist handoffs."""
    text = load_prompt("orchestrator.txt")
    for specialist in ["Triage", "Log Analyst", "Deploy Correlator", "Remediation", "Comms"]:
        assert specialist in text, f"orchestrator.txt must mention {specialist} handoff"


def test_triage_defines_all_severities() -> None:
    """Triage prompt must define P1 through P4."""
    text = load_prompt("triage.txt")
    for level in ["P1", "P2", "P3", "P4"]:
        assert level in text, f"triage.txt must define severity {level}"


def test_triage_mentions_duplicate_detection() -> None:
    """Triage prompt must describe duplicate incident detection."""
    text = load_prompt("triage.txt")
    assert "duplicate" in text.lower() or "is_duplicate" in text


def test_triage_mentions_required_tools() -> None:
    """Triage prompt must mention both its tools."""
    text = load_prompt("triage.txt")
    assert "get_service_metadata" in text
    assert "search_past_incidents" in text


def test_log_analyst_mentions_fetch_logs() -> None:
    """Log analyst prompt must instruct use of fetch_logs."""
    text = load_prompt("log_analyst.txt")
    assert "fetch_logs" in text


def test_log_analyst_output_fields() -> None:
    """Log analyst prompt must define all four output fields."""
    text = load_prompt("log_analyst.txt")
    for field in ["error_patterns", "anomaly_summary", "key_log_lines", "hypothesis"]:
        assert field in text, f"log_analyst.txt must reference output field {field!r}"


def test_deploy_correlator_mentions_list_recent_deploys() -> None:
    """Deploy correlator prompt must instruct use of list_recent_deploys."""
    text = load_prompt("deploy_correlator.txt")
    assert "list_recent_deploys" in text


def test_deploy_correlator_output_fields() -> None:
    """Deploy correlator prompt must define all four output fields."""
    text = load_prompt("deploy_correlator.txt")
    for field in ["recent_deploys", "suspect_deploy", "confidence", "evidence"]:
        assert field in text, f"deploy_correlator.txt must reference output field {field!r}"


def test_remediation_hitl_is_mandatory() -> None:
    """Remediation prompt must make HITL non-negotiable and name the tool."""
    text = load_prompt("remediation.txt")
    assert "request_human_approval" in text, (
        "remediation.txt must explicitly name the request_human_approval tool"
    )
    # Must communicate that bypassing HITL is forbidden
    lower = text.lower()
    assert "non-negotiable" in lower or "must" in lower, (
        "remediation.txt must make HITL mandatory (use 'must' or 'non-negotiable')"
    )


def test_remediation_rollback_and_hotfix_tools() -> None:
    """Remediation prompt must mention both draft tools."""
    text = load_prompt("remediation.txt")
    assert "draft_rollback_pr" in text
    assert "draft_hotfix" in text


def test_remediation_decision_tree_thresholds() -> None:
    """Remediation prompt must include confidence thresholds for decision tree."""
    text = load_prompt("remediation.txt")
    assert "0.7" in text, "remediation.txt must specify the 0.7 rollback confidence threshold"
    assert "escalate" in text.lower(), "remediation.txt must mention escalation path"


def test_comms_mentions_draft_slack_summary() -> None:
    """Comms prompt must instruct use of draft_slack_summary."""
    text = load_prompt("comms.txt")
    assert "draft_slack_summary" in text


def test_comms_required_sections() -> None:
    """Comms prompt must list all six mandatory Slack message sections."""
    text = load_prompt("comms.txt")
    for section in ["Impact", "Root Cause", "Timeline", "Current Status", "Action Items", "ETA"]:
        assert section in text, f"comms.txt must list required section {section!r}"


def test_comms_mentions_incidents_channel() -> None:
    """Comms prompt must specify the #incidents channel."""
    text = load_prompt("comms.txt")
    assert "#incidents" in text
