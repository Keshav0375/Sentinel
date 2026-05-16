"""Plain-text → Pydantic model coercion for providers without structured outputs."""

from __future__ import annotations

from pydantic import BaseModel


def coerce_output[T: BaseModel](text: str, schema: type[T]) -> T | None:
    """Parse a plain-text agent response into a Pydantic model.

    Used when structured outputs are not available (e.g. Anthropic provider).
    Strips markdown code fences, then attempts model_validate_json. Returns
    None if parsing fails so callers can fall back gracefully.

    Args:
        text: Raw text returned by the agent (may contain markdown fences).
        schema: Pydantic model class to parse into.

    Returns:
        Parsed model instance, or None if the text cannot be parsed.

    Examples:
        >>> coerce_output('{"severity": "P1", "service": "api-gw"}', MyModel)
        MyModel(severity='P1', service='api-gw')
        >>> coerce_output("```json\\n{...}\\n```", MyModel)  # fences stripped
        MyModel(...)
        >>> coerce_output("I could not determine...", MyModel)
        None
    """
    cleaned = _strip_fences(text.strip())
    try:
        return schema.model_validate_json(cleaned)
    except Exception:  # noqa: BLE001 — intentionally broad; return None on any parse failure
        return None


def _strip_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences from a string.

    Handles both ```json ... ``` and ``` ... ``` forms. The content between
    the fences is returned with leading/trailing whitespace stripped.
    """
    if not text.startswith("```"):
        return text

    lines = text.splitlines()
    # Drop the opening fence line (e.g. "```json" or "```")
    lines = lines[1:]
    # Drop the closing fence line if present
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]

    return "\n".join(lines).strip()
