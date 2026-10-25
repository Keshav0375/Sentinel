"""Tool output encoding — the text the model reads back from a tool call.

openai-agents 0.6 hands the model ``str(result)`` for any return value that is not one of
its own content types, which for a Pydantic model is a Python repr. Every Sentinel tool
therefore returns compact JSON instead: a model as an object, a list of models as an
array, ``None`` fields left out. A :class:`~sentinel.models.tools.ToolFailure` carries
``"error": true``, so a failure is recognisable whatever tool produced it.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel


def tool_json(result: BaseModel | Sequence[BaseModel]) -> str:
    """*result* as compact JSON (``None`` fields omitted) — what a tool returns to the model."""
    if isinstance(result, BaseModel):
        return result.model_dump_json(exclude_none=True)
    return "[" + ",".join(item.model_dump_json(exclude_none=True) for item in result) + "]"
