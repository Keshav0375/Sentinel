"""Episodic memory — the §5.3 ``incidents`` table, searched with pgvector.

Incidents are stored with a 384-dim embedding of their symptom text; similarity search
is ``ORDER BY embedding <=> $1`` (cosine distance) in PostgreSQL, served by the
``idx_incidents_embedding`` IVFFlat index — never a scan in Python.

Mapping (decision 2026-10-12, "Phase-1 models ↔ §5.3 schema"): the symptom text and the
timeline have no columns of their own and live in ``trajectory`` JSONB as
``{"symptoms": <text>, "timeline": <raw_timeline>, "provenance": {...}}``. Columns an
:class:`Incident` cannot supply stay NULL/default: the phase-4 pipeline passes an
:class:`~sentinel.models.memory.IncidentOutcome` for correlation_id, root_cause_confidence,
resolution_type, reflexion_loops and langfuse_trace_id (and its result, in ``trajectory`` as
``pipeline_result``); dd_event_id comes from the alert (the webhook is its source, §2.1);
pr_* and eval_score are written by later phases.
``resolved_at`` is written when the incident has one, since ``mttr_seconds`` is derived
from it.

Provenance: ``root_cause`` and ``resolution`` are agent output, not verified fact. The
stored ``provenance`` records ``root_cause_by`` / ``resolution_by`` (the agent named by the
pipeline's outcome, else ``"unknown"``), ``model_generated: true`` and ``stored_at``; it
reads back inside ``EpisodicRecord.raw_timeline["provenance"]``. The embedded symptom text
holds only what was observed (the alert: service, title, severity, and the deploy_status /
failed_stage tags when it carries them) — never an agent's hypothesis or
root cause, so recall is not keyed on earlier model output.

Read API for ``GET /incidents*`` (§3.2): :meth:`get_incident_row` /
:meth:`list_incident_rows` return the full §5.3 row (:class:`IncidentRow`, no embedding),
:meth:`stored_incident_ids` says which ids have a row, and :meth:`get_linked_deployment`
reads the ``deployments`` row whose ``incident_id`` is the incident (written by
:meth:`link_deployment`, the pipeline's backfill). :meth:`find_incident_by_correlation_id`
backs the webhook's correlation_id dedupe.

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
from pydantic import JsonValue

from sentinel.exceptions import SentinelMemoryError
from sentinel.infra import database
from sentinel.memory.base import MemoryStore
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.models.incident import Incident
from sentinel.models.memory import (
    DeploymentRow,
    EpisodicRecord,
    IncidentOutcome,
    IncidentRow,
    MemoryQueryResult,
)

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

# Every incidents column but embedding, §5.3 order (IncidentRow).
_ROW_COLUMNS = (
    "id, alert_id, dd_event_id, correlation_id, severity, service, status, root_cause, "
    "root_cause_confidence, resolution, resolution_type, pr_number, pr_url, trajectory, "
    "eval_score, langfuse_trace_id, reflexion_loops, created_at, resolved_at, mttr_seconds"
)
_DEPLOYMENT_COLUMNS = (
    "id, service, pr_number, commit_sha, author, deploy_status, gha_run_id, "
    "dd_deploy_event_id, files_changed, incident_id, deployed_at, metadata"
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

    async def store_incident(
        self, incident: Incident, *, outcome: IncidentOutcome | None = None
    ) -> None:
        """Embed the incident's observed symptoms and upsert its row into ``incidents``.

        Storing an incident with the same ID again overwrites the mapped columns, except
        ``created_at``: the first store's value is kept (when the incident was first
        recorded), and ``mttr_seconds`` is measured from it. The columns this model does
        not supply are left as they are.

        With *outcome* (the phase-4 pipeline's), its root cause, resolution and
        provenance are stored (without one: ``"unknown"``, ``"unresolved"`` and agents
        ``"unknown"``), its columns
        (correlation_id, root_cause_confidence, resolution_type, reflexion_loops,
        langfuse_trace_id) are written too, and ``trajectory`` also holds
        ``pipeline_result``.

        Raises:
            SentinelMemoryError: no database, the ID is not a UUID, or the write failed.
        """
        pool = self._require_pool()
        incident_uuid = _as_uuid(incident.id)
        if incident_uuid is None:
            raise SentinelMemoryError(f"incident id {incident.id!r} is not a UUID")

        symptoms = _build_symptom_text(incident)
        embedding = await self._embeddings.embed(symptoms)

        severity = incident.severity.value if incident.severity else incident.alert.severity
        service = incident.affected_service or incident.alert.service

        mttr_seconds: int | None = None
        if incident.resolved_at and incident.created_at:
            mttr_seconds = int((incident.resolved_at - incident.created_at).total_seconds())

        if outcome is None:
            # Only the pipeline's outcome carries a conclusion and the agent behind it.
            root_cause, root_cause_by = _NO_ROOT_CAUSE, _UNKNOWN_AGENT
            resolution, resolution_by = _NO_RESOLUTION, _UNKNOWN_AGENT
        else:
            root_cause, root_cause_by = outcome.root_cause, outcome.root_cause_by
            resolution, resolution_by = outcome.resolution, outcome.resolution_by
        raw_timeline: dict[str, Any] = {
            "id": incident.id,
            "status": incident.status.value,
            "timeline": [e.model_dump(mode="json") for e in incident.timeline],
        }
        trajectory: dict[str, Any] = {
            "symptoms": symptoms,
            "timeline": raw_timeline,
            "provenance": {
                "root_cause_by": root_cause_by,
                "resolution_by": resolution_by,
                "model_generated": True,
                "stored_at": datetime.now(UTC).isoformat(),
            },
        }
        if outcome is not None:
            trajectory["pipeline_result"] = outcome.pipeline_result
        outcome_columns, outcome_values = _outcome_columns(outcome)
        # $14… are the outcome's columns, in _OUTCOME_COLUMNS order (none without one).
        extra_names = "".join(f", {name}" for name in outcome_columns)
        extra_params = "".join(f", ${14 + i}" for i in range(len(outcome_columns)))
        extra_updates = "".join(f",\n{name} = EXCLUDED.{name}" for name in outcome_columns)

        async with database.acquire(pool) as conn:
            await conn.execute(
                f"""
                INSERT INTO incidents
                    (id, alert_id, dd_event_id, severity, service, status, root_cause,
                     resolution, trajectory, created_at, resolved_at, mttr_seconds,
                     embedding{extra_names})
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12,
                        $13{extra_params})
                ON CONFLICT (id) DO UPDATE SET
                    alert_id = EXCLUDED.alert_id,
                    dd_event_id = EXCLUDED.dd_event_id,
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
                    embedding = EXCLUDED.embedding{extra_updates}
                """,
                incident_uuid,
                incident.alert.alert_id,
                incident.alert.dd_event_id,
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
                *outcome_values,
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

        where, args = _filters(status, service)
        args.append(limit)

        async with database.acquire(pool) as conn:
            rows = await conn.fetch(
                f"SELECT {_COLUMNS} FROM incidents {where} "
                f"ORDER BY created_at DESC, id LIMIT ${len(args)}",
                *args,
            )
        return [_row_to_record(row) for row in rows]

    async def get_incident_row(self, incident_id: str) -> IncidentRow | None:
        """The full §5.3 row (no embedding) of *incident_id*, or None. A non-UUID id → None.

        Raises:
            SentinelMemoryError: no database, or the query failed.
        """
        pool = self._require_pool()
        incident_uuid = _as_uuid(incident_id)
        if incident_uuid is None:
            return None
        async with database.acquire(pool) as conn:
            row = await conn.fetchrow(
                f"SELECT {_ROW_COLUMNS} FROM incidents WHERE id = $1", incident_uuid
            )
        return None if row is None else _row_to_incident_row(row)

    async def list_incident_rows(
        self,
        *,
        status: str | None = None,
        service: str | None = None,
        limit: int = _DEFAULT_LIST_LIMIT,
    ) -> list[IncidentRow]:
        """Full rows, newest first, filtered like :meth:`list_incidents`.

        Raises:
            SentinelMemoryError: no database, or the query failed.
            ValueError: limit < 1.
        """
        pool = self._require_pool()
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")

        where, args = _filters(status, service)
        args.append(limit)

        async with database.acquire(pool) as conn:
            rows = await conn.fetch(
                f"SELECT {_ROW_COLUMNS} FROM incidents {where} "
                f"ORDER BY created_at DESC, id LIMIT ${len(args)}",
                *args,
            )
        return [_row_to_incident_row(row) for row in rows]

    async def stored_incident_ids(self, incident_ids: Sequence[str]) -> set[str]:
        """The ids among *incident_ids* (returned as given) that have an ``incidents`` row.

        Non-UUID ids never do.

        Raises:
            SentinelMemoryError: no database, or the query failed.
        """
        pool = self._require_pool()
        pairs = [(given, u) for given in incident_ids if (u := _as_uuid(given)) is not None]
        if not pairs:
            return set()
        async with database.acquire(pool) as conn:
            rows = await conn.fetch(
                "SELECT id FROM incidents WHERE id = ANY($1::uuid[])", [u for _, u in pairs]
            )
        found = {row["id"] for row in rows}
        return {given for given, u in pairs if u in found}

    async def get_linked_deployment(self, incident_id: str) -> DeploymentRow | None:
        """The ``deployments`` row whose ``incident_id`` is *incident_id* (newest if several).

        Raises:
            SentinelMemoryError: no database, or the query failed.
        """
        pool = self._require_pool()
        incident_uuid = _as_uuid(incident_id)
        if incident_uuid is None:
            return None
        async with database.acquire(pool) as conn:
            row = await conn.fetchrow(
                f"SELECT {_DEPLOYMENT_COLUMNS} FROM deployments WHERE incident_id = $1 "
                "ORDER BY deployed_at DESC NULLS LAST, id LIMIT 1",
                incident_uuid,
            )
        return None if row is None else _row_to_deployment(row)

    async def find_incident_by_correlation_id(self, correlation_id: str) -> str | None:
        """The id of the oldest stored incident with *correlation_id*, or None.

        None as well when *correlation_id* is not a UUID (the column is UUID-typed).

        Raises:
            SentinelMemoryError: no database, or the query failed.
        """
        pool = self._require_pool()
        correlation_uuid = _as_uuid(correlation_id)
        if correlation_uuid is None:
            return None
        async with database.acquire(pool) as conn:
            row = await conn.fetchrow(
                "SELECT id FROM incidents WHERE correlation_id = $1 "
                "ORDER BY created_at ASC NULLS LAST, id LIMIT 1",
                correlation_uuid,
            )
        return None if row is None else str(row["id"])

    async def link_deployment(self, incident_id: str, deploy_id: str) -> bool:
        """Set ``deployments.incident_id`` on the row *deploy_id*; False when no such row.

        Overwrites any previous link. The incident row must already exist (FK).

        Raises:
            SentinelMemoryError: no database, an id is not a UUID, or the write failed.
        """
        pool = self._require_pool()
        incident_uuid = _as_uuid(incident_id)
        if incident_uuid is None:
            raise SentinelMemoryError(f"{incident_id!r} is not a UUID")
        deploy_uuid = _as_uuid(deploy_id)
        if deploy_uuid is None:
            raise SentinelMemoryError(f"{deploy_id!r} is not a UUID")
        async with database.acquire(pool) as conn:
            status = await conn.execute(
                "UPDATE deployments SET incident_id = $1 WHERE id = $2",
                incident_uuid,
                deploy_uuid,
            )
        return status.split()[-1] != "0"

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


def _filters(status: str | None, service: str | None) -> tuple[str, list[Any]]:
    """The list filters' WHERE clause ("" for none) and its arguments, numbered from $1."""
    conditions: list[str] = []
    args: list[Any] = []
    if status is not None:
        args.append(str(status))
        conditions.append(f"status = ${len(args)}")
    if service is not None:
        args.append(service)
        conditions.append(f"service = ${len(args)}")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, args


# The incidents columns only the phase-4 pipeline supplies (IncidentOutcome), in SQL order.
_OUTCOME_COLUMNS = (
    "correlation_id",
    "root_cause_confidence",
    "resolution_type",
    "reflexion_loops",
    "langfuse_trace_id",
)


def _outcome_columns(outcome: IncidentOutcome | None) -> tuple[tuple[str, ...], list[Any]]:
    """The outcome's column names and values; none without an outcome (left as they are)."""
    if outcome is None:
        return (), []
    correlation = None if outcome.correlation_id is None else _as_uuid(outcome.correlation_id)
    return _OUTCOME_COLUMNS, [
        correlation,
        outcome.root_cause_confidence,
        outcome.resolution_type,
        outcome.reflexion_loops,
        outcome.langfuse_trace_id,
    ]


def _as_vector(value: Any) -> list[float] | None:
    """*value* as a list of floats when it is a vector (list, tuple, numpy array), else None."""
    if isinstance(value, str | bytes):
        return None
    if isinstance(value, Sequence) or hasattr(value, "__array__"):
        return [float(x) for x in value]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return None


def _build_symptom_text(incident: Incident) -> str:
    """The text embedded for similarity search: only what the alert observed.

    Service, title, severity, and the deploy_status / failed_stage tags when present.
    Agent output (triage's affected service, the analysis hypothesis, any root cause) is
    deliberately left out.
    """
    alert = incident.alert
    parts: list[str] = [f"Service {alert.service} {alert.title}", f"severity {alert.severity}"]
    for key in ("deploy_status", "failed_stage"):
        value = getattr(alert.tags, key)
        if value:
            parts.append(f"{key} {value}")
    return " ".join(parts)


# What a store without the pipeline's outcome records: no conclusion, no agent behind it.
_NO_ROOT_CAUSE = "unknown"
_NO_RESOLUTION = "unresolved"
_UNKNOWN_AGENT = "unknown"


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


def _json_column(raw: str | None) -> JsonValue:
    """A JSONB column (asyncpg returns text) decoded; NULL → None."""
    return None if raw is None else json.loads(raw)


def _uuid_text(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def _row_to_incident_row(row: asyncpg.Record) -> IncidentRow:
    """Convert an ``incidents`` row (``_ROW_COLUMNS``) to an IncidentRow."""
    return IncidentRow(
        id=str(row["id"]),
        alert_id=row["alert_id"],
        dd_event_id=row["dd_event_id"],
        correlation_id=_uuid_text(row["correlation_id"]),
        severity=row["severity"],
        service=row["service"],
        status=row["status"],
        root_cause=row["root_cause"],
        root_cause_confidence=row["root_cause_confidence"],
        resolution=row["resolution"],
        resolution_type=row["resolution_type"],
        pr_number=row["pr_number"],
        pr_url=row["pr_url"],
        trajectory=_json_column(row["trajectory"]),
        eval_score=_json_column(row["eval_score"]),
        langfuse_trace_id=row["langfuse_trace_id"],
        reflexion_loops=row["reflexion_loops"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
        mttr_seconds=row["mttr_seconds"],
    )


def _row_to_deployment(row: asyncpg.Record) -> DeploymentRow:
    """Convert a ``deployments`` row (``_DEPLOYMENT_COLUMNS``) to a DeploymentRow."""
    return DeploymentRow(
        id=str(row["id"]),
        service=row["service"],
        pr_number=row["pr_number"],
        commit_sha=row["commit_sha"],
        author=row["author"],
        deploy_status=row["deploy_status"],
        gha_run_id=row["gha_run_id"],
        dd_deploy_event_id=row["dd_deploy_event_id"],
        files_changed=_json_column(row["files_changed"]),
        incident_id=_uuid_text(row["incident_id"]),
        deployed_at=row["deployed_at"],
        metadata=_json_column(row["metadata"]),
    )


# Runtime protocol compliance check — caught at import, not first use.
assert isinstance(EpisodicMemory.__new__(EpisodicMemory), MemoryStore), (
    "EpisodicMemory does not satisfy the MemoryStore protocol"
)
