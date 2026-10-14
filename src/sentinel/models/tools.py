"""Tool I/O models — the typed inputs tools take and the structured failure they return.

A tool never raises to the agent: a call that cannot produce its result returns a
:class:`ToolFailure` saying which tool failed and why (decision 2026-10-14, "Backend
phase 3 (tools) contract"). :class:`~sentinel.exceptions.ToolError` stays the exception
class for code that does raise.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ToolFailureKind = Literal["not_found", "unavailable", "upstream_error", "bad_input"]


class ToolFailure(BaseModel):
    """A tool call that could not produce its result.

    ``kind`` tells the agent how to read the failure:

    - ``not_found``: the thing asked about does not exist (an unknown service).
    - ``unavailable``: the backing store or a credential is missing or down — no finding
      either way, so the agent must not read it as "nothing there".
    - ``upstream_error``: an external API answered with an error.
    - ``bad_input``: the call's arguments were unusable.
    """

    tool: str = Field(description="LLM-visible name of the tool that failed.")
    kind: ToolFailureKind = Field(description="Failure class: how the agent should read it.")
    message: str = Field(description="What went wrong, for the agent and the logs.")


class ServiceLookupInput(BaseModel):
    """Input of ``get_service_metadata``."""

    service_name: str = Field(description='Exact service name (e.g. "sentinel-watchtower").')
