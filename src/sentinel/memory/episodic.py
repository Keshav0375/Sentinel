"""Episodic memory — SQLite store with embedding-based similarity search."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from sentinel.infra.db import get_db
from sentinel.memory.base import MemoryStore
from sentinel.memory.embeddings import EmbeddingClient
from sentinel.models.incident import Incident
from sentinel.models.memory import EpisodicRecord, MemoryQueryResult


class EpisodicMemory:
    """SQLite-backed episodic memory with embedding similarity search.

    Stores resolved incidents indexed by an embedding of their symptom
    description. On new incident, retrieves the top-k most similar past
    incidents via cosine similarity — feeding the Triage Agent's context.

    The embedding client and db_path are injected for testability.
    """

    def __init__(self, db_path: Path, embedding_client: EmbeddingClient) -> None:
        self._db_path = db_path
        self._embeddings = embedding_client

    # ── Domain-specific API ───────────────────────────────────────────────────

    async def store_incident(self, incident: Incident) -> None:
        """Embed the incident's symptoms and persist a full record to SQLite.

        Upsert semantics — storing an incident with the same ID overwrites the
        existing row. The embedding is derived from the symptom text so that
        future similarity queries can retrieve this incident.
        """
        symptoms = _build_symptom_text(incident)
        embedding = await self._embeddings.embed(symptoms)
        embedding_blob = np.array(embedding, dtype=np.float32).tobytes()

        severity = incident.severity.value if incident.severity else incident.alert.severity.value
        service = incident.affected_service or incident.alert.service
        root_cause = _extract_root_cause(incident)
        resolution = _extract_resolution(incident)

        mttr_seconds: int | None = None
        if incident.resolved_at and incident.created_at:
            mttr_seconds = int((incident.resolved_at - incident.created_at).total_seconds())

        raw_timeline: dict[str, Any] = {
            "id": incident.id,
            "status": incident.status.value,
            "timeline": [e.model_dump(mode="json") for e in incident.timeline],
        }

        async with get_db(self._db_path) as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO episodic_incidents
                    (id, created_at, service_name, severity, symptoms,
                     root_cause, resolution, mttr_seconds, embedding, raw_timeline)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident.id,
                    incident.created_at.isoformat(),
                    service,
                    severity,
                    symptoms,
                    root_cause,
                    resolution,
                    mttr_seconds,
                    embedding_blob,
                    json.dumps(raw_timeline),
                ),
            )
            await conn.commit()

    async def search_similar(self, symptoms: str, *, top_k: int = 5) -> list[EpisodicRecord]:
        """Return the top-k most similar past incidents by symptom embedding.

        Embeds the query string, then computes cosine similarity against every
        stored embedding in Python (numpy). Suitable for MVP scale (hundreds
        of incidents); swap for a vector DB at scale.

        Args:
            symptoms: Natural language description of current incident symptoms.
            top_k: Maximum number of records to return.

        Returns:
            Records sorted descending by cosine similarity to the query.
        """
        scored = await self._scored_search(symptoms, top_k=top_k)
        return [record for _, record in scored]

    async def get_incident(self, incident_id: str) -> EpisodicRecord | None:
        """Return a single episodic record by incident ID, or None if absent."""
        async with get_db(self._db_path) as conn:
            async with conn.execute(
                "SELECT * FROM episodic_incidents WHERE id = ?", (incident_id,)
            ) as cur:
                row = await cur.fetchone()
        return None if row is None else _row_to_record(row)

    # ── MemoryStore protocol ──────────────────────────────────────────────────

    async def store(self, record: Any) -> None:
        """Persist a record. Expects an Incident instance."""
        if not isinstance(record, Incident):
            raise TypeError(f"EpisodicMemory.store() expects Incident, got {type(record).__name__}")
        await self.store_incident(record)

    async def query(self, query_input: Any, *, top_k: int = 5) -> MemoryQueryResult:
        """Similarity search returning a MemoryQueryResult with parallel scores.

        Args:
            query_input: Symptom string describing the current incident.
            top_k: Maximum number of results to return.

        Returns:
            MemoryQueryResult where records[i] and similarity_scores[i]
            correspond to the same past incident, sorted descending by score.
        """
        symptoms = str(query_input)
        scored = await self._scored_search(symptoms, top_k=top_k)
        records = [r for _, r in scored]
        scores = [s for s, _ in scored]
        return MemoryQueryResult(records=records, similarity_scores=scores)

    async def get(self, record_id: str) -> EpisodicRecord | None:
        """Return a record by primary key. None if absent."""
        return await self.get_incident(record_id)

    async def delete(self, record_id: str) -> None:
        """Remove a record by ID. No-op if the record does not exist."""
        async with get_db(self._db_path) as conn:
            await conn.execute("DELETE FROM episodic_incidents WHERE id = ?", (record_id,))
            await conn.commit()

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _scored_search(
        self, symptoms: str, *, top_k: int
    ) -> list[tuple[float, EpisodicRecord]]:
        """Embed query, load all stored embeddings, rank by cosine similarity."""
        query_vec = np.array(await self._embeddings.embed(symptoms), dtype=np.float32)
        query_norm = float(np.linalg.norm(query_vec))
        if query_norm == 0.0:
            return []

        async with get_db(self._db_path) as conn:
            async with conn.execute(
                "SELECT * FROM episodic_incidents WHERE embedding IS NOT NULL"
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            return []

        scored: list[tuple[float, EpisodicRecord]] = []
        for row in rows:
            stored_vec = np.frombuffer(row["embedding"], dtype=np.float32)
            stored_norm = float(np.linalg.norm(stored_vec))
            if stored_norm == 0.0:
                continue
            similarity = float(np.dot(query_vec, stored_vec) / (query_norm * stored_norm))
            scored.append((similarity, _row_to_record(row)))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[:top_k]


# ── Module-level helpers ──────────────────────────────────────────────────────


def _build_symptom_text(incident: Incident) -> str:
    """Build a natural language symptom string from an incident for embedding."""
    alert = incident.alert
    parts: list[str] = [
        f"Service {alert.service} {alert.metric}",
        f"current {alert.current_value} threshold {alert.threshold}",
        f"severity {alert.severity}",
    ]
    if incident.affected_service and incident.affected_service != alert.service:
        parts.append(f"affected {incident.affected_service}")
    if incident.log_analysis:
        hypothesis = incident.log_analysis.get("hypothesis")
        if hypothesis:
            parts.append(str(hypothesis))
    return " ".join(parts)


def _extract_root_cause(incident: Incident) -> str:
    """Extract a root cause string from available incident analysis results."""
    if incident.log_analysis:
        hypothesis = incident.log_analysis.get("hypothesis")
        if hypothesis:
            return str(hypothesis)
    if incident.deploy_correlation:
        evidence = incident.deploy_correlation.get("evidence")
        if evidence:
            return str(evidence)
    return "unknown"


def _extract_resolution(incident: Incident) -> str:
    """Extract a resolution string from available incident analysis results."""
    if incident.remediation_plan:
        justification = incident.remediation_plan.get("justification")
        if justification:
            return str(justification)
        action_type = incident.remediation_plan.get("action_type")
        if action_type:
            return str(action_type)
    if incident.comms_summary:
        return incident.comms_summary
    return "unresolved"


def _row_to_record(row: Any) -> EpisodicRecord:
    """Convert an aiosqlite Row to an EpisodicRecord."""
    embedding: list[float] = []
    if row["embedding"] is not None:
        embedding = np.frombuffer(row["embedding"], dtype=np.float32).tolist()

    raw_timeline: dict[str, Any] | None = None
    if row["raw_timeline"] is not None:
        raw_timeline = json.loads(row["raw_timeline"])

    created_at_raw = row["created_at"]
    if isinstance(created_at_raw, str):
        created_at = datetime.fromisoformat(created_at_raw)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
    else:
        created_at = datetime.now(UTC)

    return EpisodicRecord(
        id=row["id"],
        service_name=row["service_name"],
        severity=row["severity"],
        symptoms=row["symptoms"],
        root_cause=row["root_cause"],
        resolution=row["resolution"],
        mttr_seconds=row["mttr_seconds"],
        embedding=embedding,
        created_at=created_at,
        raw_timeline=raw_timeline,
    )


# Runtime protocol compliance check — caught at import, not first use.
assert isinstance(EpisodicMemory.__new__(EpisodicMemory), MemoryStore), (
    "EpisodicMemory does not satisfy the MemoryStore protocol"
)
