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


class EvalError(SentinelError):
    """The eval pipeline could not score a run."""
