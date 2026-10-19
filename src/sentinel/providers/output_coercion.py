"""Plain-text → Pydantic model coercion for providers without structured outputs."""

from __future__ import annotations

import json
from collections.abc import Iterator

from pydantic import BaseModel, ValidationError


def coerce_output[T: BaseModel](text: str, schema: type[T]) -> T | None:
    """Parse a plain-text agent response into a Pydantic model.

    Used when structured outputs are not available (e.g. Anthropic provider). Real models
    frame their JSON: a code fence, a sentence before it, a note after it. The whole reply
    (fences stripped) is tried first; otherwise every balanced ``{…}`` object in the reply
    is a candidate — fenced or not, with prose around it, braces inside JSON strings
    ignored — and the last one that validates as *schema* wins (a model that corrects
    itself puts the corrected object last). Top-level objects are tried before the objects
    nested inside them. Returns None if nothing validates so callers can fall back.

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

    top_level = list(_balanced_objects(text))
    for start, end in reversed(top_level):
        parsed = _validate(text[start:end], schema)
        if parsed is not None:
            return parsed
    for start, end in reversed(top_level):
        nested = _nested_objects(text, start, end)
        for inner_start, inner_end in reversed(nested):
            parsed = _validate(text[inner_start:inner_end], schema)
            if parsed is not None:
                return parsed
    return None


def _validate[T: BaseModel](candidate: str, schema: type[T]) -> T | None:
    """*candidate* as *schema* when it is a JSON object that validates, else None."""
    try:
        data: object = json.loads(candidate)
    except ValueError:
        return None
    if not isinstance(data, dict):
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


def _nested_objects(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Every object nested inside the object at ``text[start:end]``, in document order.

    Walks the outer object's JSON, skipping its strings, so only real nested objects count.
    """
    found: list[tuple[int, int]] = []
    in_string = False
    escaped = False
    i = start + 1
    while i < end - 1:
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
            inner_end = _object_end(text, i, end - 1)
            if inner_end is not None:
                found.append((i, inner_end))
                found.extend(_nested_objects(text, i, inner_end))
                i = inner_end
                continue
        i += 1
    return found


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
