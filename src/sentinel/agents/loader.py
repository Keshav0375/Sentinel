"""Prompt loader — reads agent system prompts from the prompts/ directory."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent / "prompts"


@lru_cache(maxsize=16)
def load_prompt(filename: str) -> str:
    """Load a system prompt from the prompts/ directory.

    Args:
        filename: Prompt filename, e.g. ``"orchestrator.txt"``.

    Returns:
        The prompt text with leading/trailing whitespace stripped.

    Raises:
        FileNotFoundError: If the prompt file does not exist.
    """
    path = _PROMPTS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8").strip()
