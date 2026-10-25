"""Offline contract checks for the Dockerfile (decisions 2026-10-24, phase 7 review amendments)."""

from __future__ import annotations

import re
from pathlib import Path

from sentinel.config import Settings
from sentinel.memory import embeddings

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


def _text() -> str:
    return DOCKERFILE.read_text()


def test_base_image_is_digest_pinned() -> None:
    stages = re.findall(r"^FROM\s+(\S+)\s+AS\s+(\w+)\s*$", _text(), re.M)
    assert [name for _, name in stages] == ["builder", "runtime"]
    images = {image for image, _ in stages}
    assert len(images) == 1
    assert re.fullmatch(r"python:3\.12-slim@sha256:[0-9a-f]{64}", images.pop())


def test_model_revision_matches_runtime_pin() -> None:
    match = re.search(r"SentenceTransformer\('([^']+)', revision='([0-9a-f]{40})'\)", _text())
    assert match, "the builder stage must bake the model by pinned revision"
    assert match.group(1) == Settings.model_fields["sentinel_embedding_model"].default
    assert match.group(2) == embeddings.PINNED_REVISIONS[match.group(1)]


def test_runtime_runs_offline_as_non_root() -> None:
    text = _text()
    assert "HF_HUB_OFFLINE=1" in text
    assert "USER 10001:10001" in text
