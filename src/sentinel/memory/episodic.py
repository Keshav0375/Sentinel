"""Episodic memory — the §5.3 ``incidents`` table, searched with pgvector.

Incidents are stored with a 384-dim embedding of their symptom text; similarity search
is ``ORDER BY embedding <=> $1`` (cosine distance) in PostgreSQL, served by the
``idx_incidents_embedding`` IVFFlat index — never a scan in Python.

Mapping (decision 2026-10-12, "Phase-1 models ↔ §5.3 schema"): the symptom text and the
timeline have no columns of their own and live in ``trajectory`` JSONB as
``{"symptoms": <text>, "timeline": <raw_timeline>, "provenance": {...}}``. Columns an
:class:`Incident` cannot supply (correlation_id, dd_event_id, langfuse_trace_id, pr_*,
eval_score, …) stay NULL/default until the phase-4 pipeline writes them; ``resolved_at`` is
written when the incident has one, since ``mttr_seconds`` is derived from it.

Provenance: ``root_cause`` and ``resolution`` are agent output, not verified fact. The
stored ``provenance`` records ``root_cause_by`` / ``resolution_by`` (the agent, from the
incident timeline, else ``"unknown"``), ``model_generated: true`` and ``stored_at``; it
reads back inside ``EpisodicRecord.raw_timeline["provenance"]``. The embedded symptom text
holds only what was observed (the alert: service, metric, value, threshold, severity, and a
title/description when the alert metadata carries one) — never an agent's hypothesis or
root cause, so recall is not keyed on earlier model output.

With no pool (no ``DATABASE_URL``) every method raises
:class:`~sentinel.exceptions.SentinelMemoryError` — there are no silent empty results.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

# asyncpg ships no type stubs (see infra/database.py); only its types are named here.
import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from pgvector import Vector

from sentinel.exceptions import SentinelMemoryError
from sentinel.infra import database
from sentinel.memory.base import MemoryStore
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.models.incident import Incident
from sentinel.models.memory import EpisodicRecord, MemoryQueryResult

# IVFFlat only searches `probes` of the index's lists; at the default of 1 a small or
# freshly built table can miss its nearest rows. Probing every list (= lists of
# idx_incidents_embedding, migration 001) makes the search exact.
_IVFFLAT_PROBES = 10

_DEFAULT_LIST_LIMIT = 50

_NO_POOL = "memory unavailable: no database configured (DATABASE_URL is not set)"

_COLUMNS = (
    "id, service, severity, root_cause, resolution, resolution_type, mttr_seconds, embedding, "
    "created_at, trajectory"
)


class EpisodicMemory:
    """PostgreSQL + pgvector episodic memory over the ``incidents`` table.

    Stores incidents indexed by an embedding of their symptom description. On a new
    incident, retrieves the top-k most similar past incidents by cosine similarity —
    feeding the Triage Agent's context.

    The pool and embedding client are injected. ``pool`` is None when no database is
    configured; the object still constructs, but every call raises.
    """

    def __init__(self, pool: asyncpg.Pool | None, embedding_client: EmbeddingClient) -> None:
        self._pool = pool
        self._embeddings = embedding_client

    # ── Domain-specific API ───────────────────────────────────────────────────

    async def store_incident(self, incident: Incident) -> None:
        """Embed the incident's observed symptoms and upsert its row into ``incidents``.

        Storing an incident with the same ID again overwrites the mapped columns, except
        ``created_at``: the first store's value is kept (when the incident was first
        recorded), and ``mttr_seconds`` is measured from it. The columns this model does
        not supply are left as they are.

        Raises:
            SentinelMemoryError: no database, the ID is not a UUID, or the write failed.
        """
        pool = self._require_pool()
        incident_uuid = _as_uuid(incident.id)
        if incident_uuid is None:
            raise SentinelMemoryError(f"incident id {incident.id!r} is not a UUID")

        symptoms = _build_symptom_text(incident)
        embedding = await self._embeddings.embed(symptoms)

        severity = incident.severity.value if incident.severity else incident.alert.severity.value
        service = incident.affected_service or incident.alert.service

        mttr_seconds: int | None = None
        if incident.resolved_at and incident.created_at:
            mttr_seconds = int((incident.resolved_at - incident.created_at).total_seconds())

        root_cause, root_cause_role = _root_cause_with_source(incident)
        resolution, resolution_role = _resolution_with_source(incident)
        raw_timeline: dict[str, Any] = {
            "id": incident.id,
            "status": incident.status.value,
            "timeline": [e.model_dump(mode="json") for e in incident.timeline],
        }
        trajectory = {
            "symptoms": symptoms,
            "timeline": raw_timeline,
            "provenance": {
                "root_cause_by": _agent_for_role(incident, root_cause_role),
                "resolution_by": _agent_for_role(incident, resolution_role),
                "model_generated": True,
                "stored_at": datetime.now(UTC).isoformat(),
            },
        }

        async with database.acquire(pool) as conn:
            await conn.execute(
                """
                INSERT INTO incidents
                    (id, alert_id, severity, service, status, root_cause, resolution,
                     trajectory, created_at, resolved_at, mttr_seconds, embedding)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12)
                ON CONFLICT (id) DO UPDATE SET
                    alert_id = EXCLUDED.alert_id,
                    severity = EXCLUDED.severity,
                    service = EXCLUDED.service,
                    status = EXCLUDED.status,
                    root_cause = EXCLUDED.root_cause,
                    resolution = EXCLUDED.resolution,
                    trajectory = EXCLUDED.trajectory,
                    -- created_at is NOT updated: the first store's value is kept.
                    resolved_at = EXCLUDED.resolved_at,
                    mttr_seconds = CASE
                        WHEN EXCLUDED.resolved_at IS NULL OR incidents.created_at IS NULL
                            THEN EXCLUDED.mttr_seconds
                        ELSE FLOOR(EXTRACT(EPOCH FROM
                            EXCLUDED.resolved_at - incidents.created_at))::int
                    END,
                    embedding = EXCLUDED.embedding
                """,
                incident_uuid,
                incident.alert.alert_id,
                severity,
                service,
                incident.status.value,
                root_cause,
                resolution,
                json.dumps(trajectory),
                incident.created_at,
                incident.resolved_at,
                mttr_seconds,
                Vector(embedding),
            )

    async def search_similar(self, symptoms: str, *, top_k: int = 5) -> list[EpisodicRecord]:
        """Return the top-k past incidents most similar to *symptoms*.

        Embeds the query text, then ranks by pgvector cosine distance.

        Args:
            symptoms: Natural language description of current incident symptoms.
            top_k: Maximum number of records to return.

        Returns:
            Records sorted descending by cosine similarity to the query.
        """
        self._require_pool()
        result = await self.search_by_embedding(await self._embeddings.embed(symptoms), top_k=top_k)
        return result.records

    async def search_by_embedding(
        self, embedding: list[float], *, top_k: int = 5
    ) -> MemoryQueryResult:
        """Return the top-k incidents nearest to a caller-supplied embedding.

        ``ORDER BY embedding <=> $1 LIMIT $2`` inside a transaction that first sets
        ``ivfflat.probes``; similarity is ``1 - cosine distance``. Rows whose distance is
        undefined (a zero vector on either side) are left out.

        Args:
            embedding: Query vector — 384 floats for the §5.3 schema.
            top_k: Maximum number of results (>= 1).

        Returns:
            MemoryQueryResult whose records and similarity_scores are parallel and
            sorted descending by similarity.

        Raises:
            SentinelMemoryError: no database, or the query failed (e.g. wrong dimensions).
            ValueError: top_k < 1.
        """
        pool = self._require_pool()
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1, got {top_k}")

        async with database.acquire(pool) as conn, conn.transaction():
            await conn.execute(f"SET LOCAL ivfflat.probes = {_IVFFLAT_PROBES}")
            rows = await conn.fetch(
                f"""
                SELECT {_COLUMNS}, embedding <=> $1 AS distance
                FROM incidents
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> $1
                LIMIT $2
                """,
                Vector(embedding),
                top_k,
            )

        records: list[EpisodicRecord] = []
        scores: list[float] = []
        for row in rows:
            distance = float(row["distance"])
            if math.isnan(distance):
                continue
            records.append(_row_to_record(row))
            scores.append(1.0 - distance)
        return MemoryQueryResult(records=records, similarity_scores=scores)

    async def get_incident(self, incident_id: str) -> EpisodicRecord | None:
        """Return a single episodic record by incident ID, or None if absent.

        An ID that is not a UUID cannot exist in ``incidents`` and returns None.
        """
        pool = self._require_pool()
        incident_uuid = _as_uuid(incident_id)
        if incident_uuid is None:
            return None
        async with database.acquire(pool) as conn:
            row = await conn.fetchrow(
                f"SELECT {_COLUMNS} FROM incidents WHERE id = $1", incident_uuid
            )
        return None if row is None else _row_to_record(row)

    async def list_incidents(
        self,
        *,
        status: str | None = None,
        service: str | None = None,
        limit: int = _DEFAULT_LIST_LIMIT,
    ) -> list[EpisodicRecord]:
        """Return incidents, newest first, optionally filtered by status and/or service.

        Args:
            status: Exact ``incidents.status`` value (an ``IncidentStatus`` works).
            service: Exact ``incidents.service`` name.
            limit: Maximum number of records (>= 1).

        Raises:
            SentinelMemoryError: no database, or the query failed.
            ValueError: limit < 1.
        """
        pool = self._require_pool()
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")

        conditions: list[str] = []
        args: list[Any] = []
        if status is not None:
            args.append(str(status))
            conditions.append(f"status = ${len(args)}")
        if service is not None:
            args.append(service)
            conditions.append(f"service = ${len(args)}")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        args.append(limit)

        async with database.acquire(pool) as conn:
            rows = await conn.fetch(
                f"SELECT {_COLUMNS} FROM incidents {where} "
                f"ORDER BY created_at DESC, id LIMIT ${len(args)}",
                *args,
            )
        return [_row_to_record(row) for row in rows]

    # ── MemoryStore protocol ──────────────────────────────────────────────────

    async def store(self, record: Any) -> None:
        """Persist a record. Expects an Incident instance."""
        if not isinstance(record, Incident):
            raise TypeError(f"EpisodicMemory.store() expects Incident, got {type(record).__name__}")
        await self.store_incident(record)

    async def query(self, query_input: Any, *, top_k: int = 5) -> MemoryQueryResult:
        """Similarity search returning a MemoryQueryResult with parallel scores.

        Args:
            query_input: Symptom text describing the current incident, or a ready
                embedding vector — any sequence of floats (list, tuple, numpy array).
            top_k: Maximum number of results to return.

        Returns:
            MemoryQueryResult where records[i] and similarity_scores[i]
            correspond to the same past incident, sorted descending by score.
        """
        self._require_pool()
        embedding = _as_vector(query_input)
        if embedding is None:
            embedding = await self._embeddings.embed(str(query_input))
        return await self.search_by_embedding(embedding, top_k=top_k)

    async def get(self, record_id: str) -> EpisodicRecord | None:
        """Return a record by primary key. None if absent."""
        return await self.get_incident(record_id)

    async def delete(self, record_id: str) -> None:
        """Remove a record by ID. No-op if the record does not exist."""
        pool = self._require_pool()
        incident_uuid = _as_uuid(record_id)
        if incident_uuid is None:
            return
        async with database.acquire(pool) as conn:
            await conn.execute("DELETE FROM incidents WHERE id = $1", incident_uuid)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise SentinelMemoryError(_NO_POOL)
        return self._pool


# ── Module-level helpers ──────────────────────────────────────────────────────


def _as_uuid(value: str) -> uuid.UUID | None:
    """*value* as a UUID, or None when it is not one."""
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _as_vector(value: Any) -> list[float] | None:
    """*value* as a list of floats when it is a vector (list, tuple, numpy array), else None."""
    if isinstance(value, str | bytes):
        return None
    if isinstance(value, Sequence) or hasattr(value, "__array__"):
        return [float(x) for x in value]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return None


def _build_symptom_text(incident: Incident) -> str:
    """The text embedded for similarity search: only what the alert observed.

    Service, metric, current value vs threshold, severity, and the alert's title /
    description when its metadata carries them. Agent output (triage's affected service,
    the log analyst's hypothesis, any root cause) is deliberately left out.
    """
    alert = incident.alert
    parts: list[str] = [
        f"Service {alert.service} {alert.metric}",
        f"current {alert.current_value} threshold {alert.threshold}",
        f"severity {alert.severity}",
    ]
    for key in ("title", "description"):
        value = alert.metadata.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " ".join(parts)


# Agent roles, matched against the agent names in the incident timeline.
_LOG_ANALYST = "log_analyst"
_DEPLOY_CORRELATOR = "deploy_correlator"
_REMEDIATION = "remediation"
_COMMS = "comms"
_HANDOFF_PREFIX = "handoff → "


def _root_cause_with_source(incident: Incident) -> tuple[str, str | None]:
    """The root cause from the analysis results, and the role of the agent that produced it."""
    if incident.log_analysis:
        hypothesis = incident.log_analysis.get("hypothesis")
        if hypothesis:
            return str(hypothesis), _LOG_ANALYST
    if incident.deploy_correlation:
        evidence = incident.deploy_correlation.get("evidence")
        if evidence:
            return str(evidence), _DEPLOY_CORRELATOR
    return "unknown", None


def _resolution_with_source(incident: Incident) -> tuple[str, str | None]:
    """The resolution from the analysis results, and the role of the agent that produced it."""
    if incident.remediation_plan:
        justification = incident.remediation_plan.get("justification")
        if justification:
            return str(justification), _REMEDIATION
        action_type = incident.remediation_plan.get("action_type")
        if action_type:
            return str(action_type), _REMEDIATION
    if incident.comms_summary:
        return incident.comms_summary, _COMMS
    return "unresolved", None


def _agent_for_role(incident: Incident, role: str | None) -> str:
    """The timeline's name for the agent in *role* (latest entry wins), else "unknown".

    An agent shows up as an entry's ``agent_name`` or as the target of a handoff
    (``"handoff → <agent>"``).
    """
    if role is None:
        return "unknown"
    for entry in reversed(incident.timeline):
        if role in entry.agent_name:
            return entry.agent_name
        if entry.action.startswith(_HANDOFF_PREFIX):
            target = entry.action.removeprefix(_HANDOFF_PREFIX).strip()
            if role in target:
                return target
    return "unknown"


def _row_to_record(row: asyncpg.Record) -> EpisodicRecord:
    """Convert an ``incidents`` row to an EpisodicRecord.

    Rows written by other writers (the phase-4 pipeline, GHA) may lack the trajectory
    or the analysis columns; those read back as empty text / "unknown" / "unresolved".
    The trajectory's ``provenance`` is exposed as ``raw_timeline["provenance"]``.
    """
    trajectory_raw: str | None = row["trajectory"]
    trajectory: dict[str, Any] = {}
    if trajectory_raw is not None:
        loaded = json.loads(trajectory_raw)
        if isinstance(loaded, dict):
            trajectory = loaded  # pyright: ignore[reportUnknownVariableType]

    timeline = trajectory.get("timeline")
    raw_timeline: dict[str, Any] | None = timeline if isinstance(timeline, dict) else None  # pyright: ignore[reportUnknownVariableType]
    provenance = trajectory.get("provenance")
    if isinstance(provenance, dict):
        raw_timeline = {**(raw_timeline or {}), "provenance": provenance}

    vector: Vector | None = row["embedding"]
    fields: dict[str, Any] = {
        "id": str(row["id"]),
        "service_name": row["service"],
        "severity": row["severity"],
        "symptoms": str(trajectory.get("symptoms") or ""),
        "root_cause": row["root_cause"] or "unknown",
        "resolution": row["resolution"] or "unresolved",
        "resolution_type": row["resolution_type"],
        "mttr_seconds": row["mttr_seconds"],
        "embedding": vector.to_list() if vector is not None else [],
        "raw_timeline": raw_timeline,
    }
    if row["created_at"] is not None:
        fields["created_at"] = row["created_at"]
    return EpisodicRecord(**fields)


# Runtime protocol compliance check — caught at import, not first use.
assert isinstance(EpisodicMemory.__new__(EpisodicMemory), MemoryStore), (
    "EpisodicMemory does not satisfy the MemoryStore protocol"
)
