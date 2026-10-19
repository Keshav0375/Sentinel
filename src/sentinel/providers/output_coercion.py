"""Plain-text → Pydantic model coercion for providers without structured outputs."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

from pydantic import BaseModel, ValidationError


def coerce_output[T: BaseModel](text: str, schema: type[T]) -> T | None:
    """Parse a plain-text agent response into a Pydantic model.

    Used when structured outputs are not available (e.g. Anthropic provider). Real models
    frame their JSON (a sentence before it, a code fence, a note after it) and may quote
    other JSON — echoed tool data — around their answer. The candidates, in order:

    1. the whole reply as JSON (one enclosing code fence stripped);
    2. the LAST fenced ```json block that validates;
    3. the FIRST top-level ``{…}`` object in the reply that validates (braces inside JSON
       strings ignored; objects nested in another object are never candidates).

    In steps 2 and 3 a schema with an ``is_explicit_reply(data)`` classmethod (one whose
    validation fills defaults, e.g. ``ReflexionResult``) only accepts an object that states
    every gate-relevant key itself, so a stray quoted object is never taken as the answer.
    Returns None if nothing validates so callers can fall back.

    Args:
        text: Raw text returned by the agent (may contain markdown fences and prose).
        schema: Pydantic model class to parse into.

    Returns:
        Parsed model instance, or None if the text cannot be parsed.

    Examples:
        >>> coerce_output('{"severity": "P1", "service": "api-gw"}', MyModel)
        MyModel(severity='P1', service='api-gw')
        >>> coerce_output("Here is my critique:\\n```json\\n{...}\\n```\\nNote: ...", MyModel)
        MyModel(...)
        >>> coerce_output("I could not determine...", MyModel)
        None
    """
    cleaned = _strip_fences(text.strip())
    try:
        return schema.model_validate_json(cleaned)
    except ValueError:  # ValidationError subclasses it; anything malformed lands here
        pass

    for block in reversed(_JSON_FENCE.findall(text)):
        parsed = _validate(block, schema)
        if parsed is not None:
            return parsed
    for start, end in _balanced_objects(text):
        parsed = _validate(text[start:end], schema)
        if parsed is not None:
            return parsed
    return None


# A ```json fenced block (any case), inline or on its own lines; group 1 is its content.
_JSON_FENCE = re.compile(r"```json[ \t]*\n?(.*?)```", re.DOTALL | re.IGNORECASE)


def _validate[T: BaseModel](candidate: str, schema: type[T]) -> T | None:
    """*candidate* as *schema* when it is a JSON object that validates (and, for a schema
    with ``is_explicit_reply``, states its gate-relevant keys itself), else None."""
    try:
        data: object = json.loads(candidate)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    explicit = getattr(schema, "is_explicit_reply", None)
    if callable(explicit) and not explicit(data):
        return None
    try:
        return schema.model_validate(data)
    except ValidationError:
        return None


def _balanced_objects(text: str) -> Iterator[tuple[int, int]]:
    """``(start, end)`` spans of the top-level balanced ``{…}`` objects in *text*.

    Inside an object, double-quoted strings (with backslash escapes) are skipped, so a
    brace in a JSON string value does not count. Outside one — prose — quotes mean nothing.
    An opening brace that never closes is skipped and the scan resumes after it.
    """
    stop = len(text)
    i = 0
    while i < stop:
        if text[i] != "{":
            i += 1
            continue
        end = _object_end(text, i, stop)
        if end is None:
            i += 1
            continue
        yield i, end
        i = end


def _object_end(text: str, start: int, stop: int) -> int | None:
    """The index just past the ``}`` closing the object opened at *start*, or None."""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, stop):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i + 1
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
