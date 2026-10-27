"""ledger.append against a local bare remote: bootstrap, idempotency, races, bounded retry (10.1).

The remote is always `git init --bare` under tmp_path; nothing here talks to GitHub.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = REPO_ROOT / "scripts/ops/ledger.py"

MakeEvent = Callable[..., dict[str, Any]]


def _race(
    ledger: ModuleType, monkeypatch: pytest.MonkeyPatch, other: dict[str, Any], remote_uri: str
) -> list[int]:
    """Make the first push lose: another writer appends `other` just before it."""
    orig_push = ledger._push
    orig_append = ledger.append
    state = {"raced": False, "inner": False}
    calls: list[int] = []

    def racing_push(git: Any, remote: str, commit: str, branch: str) -> bool:
        if state["inner"]:
            return bool(orig_push(git, remote, commit, branch))
        calls.append(1)
        if not state["raced"]:
            state["raced"] = True
            state["inner"] = True
            try:
                inner = orig_append(other, remote=remote_uri, sleep=lambda s: None)
            finally:
                state["inner"] = False
            assert inner.status == "written"
        return bool(orig_push(git, remote, commit, branch))

    monkeypatch.setattr(ledger, "_push", racing_push)
    return calls


def test_append_bootstraps_orphan_branch(
    ledger: ModuleType, make_event: MakeEvent, remote: Any
) -> None:
    event = make_event()
    result = ledger.append(event, remote=remote.uri)
    assert result.status == "written"
    assert result.attempts == 1
    assert result.event_id == event["event_id"]
    path = ledger.event_path(event)
    assert result.path == path

    assert remote.files() == {
        "README.md",
        "schema/event.v1.json",
        "schema/snapshot.v1.json",
        path,
    }
    assert remote.git("rev-list", "--count", "ops-ledger") == "2"
    roots = remote.git("rev-list", "--max-parents=0", "ops-ledger").splitlines()
    assert len(roots) == 1
    assert not any(p.startswith("events/") for p in remote.files(roots[0]))
    assert remote.git_bytes("show", "ops-ledger:schema/event.v1.json") == (
        ledger.EVENT_SCHEMA_PATH.read_bytes()
    )
    assert remote.git_bytes("show", "ops-ledger:schema/snapshot.v1.json") == (
        ledger.SNAPSHOT_SCHEMA_PATH.read_bytes()
    )
    assert remote.git_bytes("show", "ops-ledger:README.md") == ledger.LEDGER_README.encode()
    assert remote.git_bytes("show", f"ops-ledger:{path}") == ledger.serialize(event)
    log = remote.git("log", "-1", "--format=%an%n%ae%n%s", "ops-ledger").splitlines()
    assert log == [
        ledger.BOT_NAME,
        ledger.BOT_EMAIL,
        f"ops: deploy.failed {event['event_id'][:12]}",
    ]
    assert remote.git("log", "-1", "--format=%s", roots[0]) == "ops-ledger: initialise"


def test_append_is_idempotent(ledger: ModuleType, make_event: MakeEvent, remote: Any) -> None:
    event = make_event()
    assert ledger.append(event, remote=remote.uri).status == "written"
    again = ledger.append(event, remote=remote.uri)
    assert again.status == "exists"
    assert again.attempts == 1
    assert remote.git("rev-list", "--count", "ops-ledger") == "2"


def test_append_dedupes_by_event_id_across_paths(
    ledger: ModuleType, make_event: MakeEvent, remote: Any
) -> None:
    first = make_event()
    later = make_event(ts="2026-10-26T09:00:00Z")
    assert ledger.event_path(first) != ledger.event_path(later)
    assert ledger.append(first, remote=remote.uri).status == "written"
    assert ledger.append(later, remote=remote.uri).status == "exists"
    assert remote.events() == [ledger.event_path(first)]


def test_append_stacks_events_linearly(
    ledger: ModuleType, make_event: MakeEvent, remote: Any
) -> None:
    one = make_event(source_id="1.1")
    two = make_event(source_id="2.1")
    assert ledger.append(one, remote=remote.uri).status == "written"
    assert ledger.append(two, remote=remote.uri).status == "written"
    assert remote.git("rev-list", "--count", "ops-ledger") == "3"
    assert remote.events() == sorted([ledger.event_path(one), ledger.event_path(two)])
    assert len(remote.git("rev-list", "--max-parents=0", "ops-ledger").splitlines()) == 1
    assert len(remote.git("rev-list", "--merges", "ops-ledger").splitlines()) == 0


def test_append_retries_after_push_race(
    ledger: ModuleType, make_event: MakeEvent, remote: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert ledger.append(make_event(source_id="0.1"), remote=remote.uri).status == "written"
    other = make_event(source_id="1.1")
    mine = make_event(source_id="2.1")
    calls = _race(ledger, monkeypatch, other, remote.uri)
    sleeps: list[float] = []
    result = ledger.append(mine, remote=remote.uri, sleep=sleeps.append)
    assert result.status == "written"
    assert result.attempts == 2
    assert len(calls) == 2
    assert len(sleeps) == 1
    assert 1 <= sleeps[0] <= 1.5
    assert {ledger.event_path(other), ledger.event_path(mine)} <= set(remote.events())
    assert remote.git("rev-list", "--count", "ops-ledger") == "4"


def test_append_concurrent_bootstrap_converges(
    ledger: ModuleType, make_event: MakeEvent, remote: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = make_event(source_id="1.1")
    mine = make_event(source_id="2.1")
    _race(ledger, monkeypatch, other, remote.uri)
    result = ledger.append(mine, remote=remote.uri, sleep=lambda s: None)
    assert result.status == "written"
    assert result.attempts == 2
    assert len(remote.git("rev-list", "--max-parents=0", "ops-ledger").splitlines()) == 1
    assert remote.events() == sorted([ledger.event_path(other), ledger.event_path(mine)])
    assert remote.git("rev-list", "--count", "ops-ledger") == "3"


def test_append_gives_up_after_bounded_attempts(
    ledger: ModuleType, make_event: MakeEvent, remote: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pushes: list[str] = []

    def rejected(git: Any, remote_url: str, commit: str, branch: str) -> bool:
        pushes.append(commit)
        return False

    monkeypatch.setattr(ledger, "_push", rejected)
    sleeps: list[float] = []
    with pytest.raises(ledger.LedgerPushError) as caught:
        ledger.append(make_event(), remote=remote.uri, max_attempts=3, sleep=sleeps.append)
    assert len(pushes) == 3
    assert len(sleeps) == 2
    assert 1 <= sleeps[0] <= 1.5
    assert 2 <= sleeps[1] <= 2.5
    assert "after 3 attempts" in str(caught.value)
    assert remote.git("ls-remote", "--heads", str(remote.path)) == ""


def test_append_retries_on_git_failure_then_raises(
    ledger: ModuleType, make_event: MakeEvent, tmp_path: Path, git_bin: str
) -> None:
    sleeps: list[float] = []
    missing = (tmp_path / "missing.git").as_uri()
    with pytest.raises(ledger.LedgerPushError) as caught:
        ledger.append(make_event(), remote=missing, max_attempts=2, sleep=sleeps.append)
    assert len(sleeps) == 1
    assert "git ls-remote failed" in str(caught.value)


def test_append_rejects_invalid_or_secret_event(
    ledger: ModuleType, make_event: MakeEvent, remote: Any
) -> None:
    invalid = make_event()
    invalid["data"]["log"] = "x"
    with pytest.raises(ledger.SchemaValidationError):
        ledger.append(invalid, remote=remote.uri)

    leaky = make_event()
    leaky["data"]["version"] = "gh" + "p_" + "A" * 36
    with pytest.raises(ledger.SecretDetectedError) as caught:
        ledger.append(leaky, remote=remote.uri)
    assert caught.value.paths == ["data.version"]
    assert remote.git("ls-remote", "--heads", str(remote.path)) == ""


def test_git_env_carries_token_in_header_only(
    ledger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", "/tmp/elsewhere")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.sshCommand=evil'")
    with_token = ledger._git_env("t0k")
    assert with_token["GIT_CONFIG_COUNT"] == "1"
    assert with_token["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert with_token["GIT_CONFIG_VALUE_0"] == (
        "AUTHORIZATION: basic " + base64.b64encode(b"x-access-token:t0k").decode()
    )
    without = ledger._git_env(None)
    assert "GIT_CONFIG_COUNT" not in without
    for env in (with_token, without):
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == ledger.BOT_NAME
        assert not any(key.startswith("GIT_DIR") for key in env)
        assert "GIT_CONFIG_PARAMETERS" not in env
        assert not any("t0k" in value for key, value in env.items() if key != "GIT_CONFIG_VALUE_0")


def test_git_errors_redact_the_token(ledger: ModuleType, tmp_path: Path, git_bin: str) -> None:
    git = ledger._Git(tmp_path, "s3cr3t-tok")
    git.run("init", "-q")
    b64 = base64.b64encode(b"x-access-token:s3cr3t-tok").decode()
    with pytest.raises(ledger.LedgerGitError) as caught:
        git.run("cat-file", "-t", f"s3cr3t-tok-{b64}")
    assert "s3cr3t-tok" not in str(caught.value)
    assert b64 not in str(caught.value)
    assert str(caught.value).startswith("git cat-file failed: ")
    assert str(caught.value).endswith("***-***")


def test_ledger_never_force_pushes() -> None:
    text = LEDGER_PATH.read_text()
    assert "--force" not in text
    assert "+{commit}" not in text


def test_op_timeout_caps_at_deadline(ledger: ModuleType) -> None:
    assert ledger.op_timeout("push", None, 0) == 30
    assert ledger.op_timeout("write-tree", None, 0) == 10
    assert ledger.op_timeout("fetch", 100.0, 95.0) == 5.0
    with pytest.raises(ledger.LedgerGitError, match="deadline"):
        ledger.op_timeout("push", 100.0, 100.0)


def test_append_stops_at_deadline(
    ledger: ModuleType, make_event: MakeEvent, remote: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 0.0}

    def tick() -> float:
        clock["now"] += 50.0
        return clock["now"]

    pushes: list[str] = []

    def rejected(git: Any, remote_url: str, commit: str, branch: str) -> bool:
        pushes.append(commit)
        return False

    monkeypatch.setattr(ledger, "_monotonic", tick)
    monkeypatch.setattr(ledger, "_push", rejected)
    sleeps: list[float] = []
    with pytest.raises(ledger.LedgerPushError) as caught:
        ledger.append(make_event(), remote=remote.uri, max_attempts=10, sleep=sleeps.append)
    assert "deadline" in str(caught.value)
    assert len(pushes) < 10
    assert all(s <= ledger.APPEND_DEADLINE_S for s in sleeps)


def test_push_rejected_by_hook_fails_fast(
    ledger: ModuleType, make_event: MakeEvent, remote: Any
) -> None:
    hook = remote.path / "hooks/pre-receive"
    hook.write_text('#!/bin/sh\necho "GH013: Repository rule violations found" >&2\nexit 1\n')
    hook.chmod(0o755)
    sleeps: list[float] = []
    with pytest.raises(ledger.LedgerPushRejectedError) as caught:
        ledger.append(
            make_event(), remote=remote.uri, token="s3cr3t-tok", max_attempts=5, sleep=sleeps.append
        )
    assert sleeps == []
    assert "rejected" in str(caught.value)
    assert "s3cr3t-tok" not in str(caught.value)
    assert isinstance(caught.value, ledger.LedgerPushError)  # CLI exit 3 still applies
    assert remote.git("ls-remote", "--heads", str(remote.path)) == ""


def test_push_non_fast_forward_retries(
    ledger: ModuleType, make_event: MakeEvent, remote: Any, tmp_path: Path
) -> None:
    """A push whose parent is behind the tip is a race (False → rebuild and retry), not fatal."""
    assert ledger.append(make_event(source_id="1.1"), remote=remote.uri).status == "written"
    work = tmp_path / "stale"
    work.mkdir()
    git = ledger._Git(work, None)
    git.run("init", "-q")
    tip = ledger._fetch_tip(git, remote.uri, ledger.LEDGER_BRANCH)
    git.run("read-tree", tip)
    ledger._add_blob(git, "events/stale.json", b"{}")
    stale = ledger._commit(git, tip, "stale")
    assert ledger.append(make_event(source_id="2.1"), remote=remote.uri).status == "written"

    assert ledger._push(git, remote.uri, stale, ledger.LEDGER_BRANCH) is False
    assert "events/stale.json" not in remote.files()
