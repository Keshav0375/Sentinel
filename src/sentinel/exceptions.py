"""Sentinel exception hierarchy.

Every error Sentinel raises on purpose derives from :class:`SentinelError`, so callers
can catch the whole family in one place. Library and driver exceptions are wrapped at
the boundary that owns them (``raise SentinelMemoryError(...) from exc``) — they never
leak out under their own names.

The memory/storage error is ``SentinelMemoryError``, never ``MemoryError``: that name
is a Python builtin (out-of-memory) and shadowing it would make ``except MemoryError``
ambiguous.
"""

from __future__ import annotations


class SentinelError(Exception):
    """Base class for all Sentinel errors."""


class ToolError(SentinelError):
    """A tool could not complete its call (bad input, missing data, upstream failure)."""


class SentinelMemoryError(SentinelError):
    """A memory/storage operation failed — connection, pool, query, or codec."""


class AgentError(SentinelError):
    """An agent run failed or produced output that violates its contract."""


class ProviderUnavailableError(AgentError):
    """No LLM provider can serve a call: its credential (and the fallback's) is not set."""


class ToolBudgetExhausted(AgentError):
    """The incident's tool-call budget (``sentinel_max_tool_calls``, §4.6) is spent.

    Raised by the orchestrator's budget guard to stop an agent run after a tool call was
    refused; the orchestrator escalates with the context gathered so far.
    """


class EvalError(SentinelError):
    """The eval pipeline could not score a run."""
