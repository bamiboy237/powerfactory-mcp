"""SQLite source of truth for persisted model-graph extractions."""

from __future__ import annotations

from datetime import timezone
from typing import Iterable

from powerfactory_agent.domain.topology import (
    ExtractionProvenance,
    GraphAsset,
    GraphAttribute,
    GraphIncrementalRefresh,
    GraphRelationship,
    GraphSnapshot,
)
from powerfactory_agent.serialization import canonical_json, from_json

from .database import SQLiteDatabase


class GraphContextMismatchError(ValueError):
    """A caller attempted to use persisted records for another configuration."""


class GraphSnapshotNotFoundError(LookupError):
    pass


class ModelGraphStore:
    """Persist normalized graph records and restore snapshots without a gateway."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def record_full(self, snapshot: GraphSnapshot) -> GraphSnapshot:
        self._insert(snapshot, mode="full")
        return snapshot

    def record_incremental(self, refresh: GraphIncrementalRefresh) -> GraphSnapshot:
        prior = self.latest(context_id=refresh.context.model_context_id)
        if prior.context.configuration_key != refresh.context.configuration_key:
            raise GraphContextMismatchError("incremental refresh configuration key does not match stored context")
        if refresh.context.extraction_revision.counter <= prior.context.extraction_revision.counter:
            raise ValueError("incremental extraction revision must advance")
        prior_assets = {item.asset.product_identity.value: item for item in prior.assets}
        for changed in refresh.changed_assets:
            prior_assets[changed.asset.product_identity.value] = changed
        if set(prior_assets) != {item.product_identity.value for item in refresh.context.assets}:
            raise GraphContextMismatchError("incremental context asset set does not match stored context")
        attributes = {
            (item.asset_identity.value, item.name): item for item in prior.attributes
        }
        changed_asset_ids = {item.asset.product_identity.value for item in refresh.changed_assets}
        for key in tuple(attributes):
            if key[0] in changed_asset_ids:
                del attributes[key]
        for item in refresh.changed_attributes:
            attributes[(item.asset_identity.value, item.name)] = item
        relationships = {item.relationship_id: item for item in prior.relationships}
        for relationship_id in refresh.removed_relationship_ids:
            relationships.pop(relationship_id, None)
        for item in refresh.changed_relationships:
            relationships[item.relationship_id] = item
        assets = tuple(sorted(prior_assets.values(), key=lambda item: item.asset.product_identity.value))
        snapshot = GraphSnapshot(
            run_id=refresh.run_id,
            context=refresh.context,
            extraction_fingerprint=refresh.extraction_fingerprint,
            assets=assets,
            attributes=tuple(sorted(attributes.values(), key=lambda item: (item.asset_identity.value, item.name))),
            relationships=tuple(sorted(relationships.values(), key=lambda item: item.relationship_id)),
            provenance=refresh.provenance,
        )
        self._insert(snapshot, mode="incremental")
        return snapshot

    def latest(self, *, context_id: str | None = None, expected_configuration_key: str | None = None) -> GraphSnapshot:
        query = (
            "SELECT graph_extraction_runs.snapshot_json, graph_contexts.configuration_key "
            "FROM graph_extraction_runs JOIN graph_contexts USING(model_context_id)"
        )
        parameters: list[object] = []
        if context_id is not None:
            query += " WHERE model_context_id = ?"
            parameters.append(context_id)
        query += " ORDER BY graph_extraction_runs.recorded_at DESC, graph_extraction_runs.extraction_counter DESC LIMIT 1"
        with self.database.connect() as connection:
            row = connection.execute(query, tuple(parameters)).fetchone()
        if row is None:
            raise GraphSnapshotNotFoundError(context_id or "latest")
        if expected_configuration_key is not None and row["configuration_key"] != expected_configuration_key:
            raise GraphContextMismatchError("active configuration does not match persisted graph context")
        return from_json(GraphSnapshot, row["snapshot_json"])

    def revision(self, *, context_id: str, counter: int) -> GraphSnapshot:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM graph_extraction_runs WHERE model_context_id = ? AND extraction_counter = ?",
                (context_id, counter),
            ).fetchone()
        if row is None:
            raise GraphSnapshotNotFoundError(f"{context_id}:{counter}")
        return from_json(GraphSnapshot, row["snapshot_json"])

    def _insert(self, snapshot: GraphSnapshot, *, mode: str) -> None:
        recorded_at = snapshot.context.extracted_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        context_json = canonical_json(snapshot.context)
        snapshot_json = canonical_json(snapshot)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO graph_contexts(model_context_id, configuration_key, extraction_counter, context_json, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(model_context_id) DO UPDATE SET configuration_key=excluded.configuration_key,
                    extraction_counter=excluded.extraction_counter, context_json=excluded.context_json,
                    recorded_at=excluded.recorded_at""",
                (
                    snapshot.context.model_context_id,
                    snapshot.context.configuration_key.value,
                    snapshot.context.extraction_revision.counter,
                    context_json,
                    recorded_at,
                ),
            )
            connection.execute(
                """INSERT INTO graph_extraction_runs(run_id, model_context_id, extraction_counter, fingerprint, mode, snapshot_json, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.run_id,
                    snapshot.context.model_context_id,
                    snapshot.context.extraction_revision.counter,
                    snapshot.extraction_fingerprint.value,
                    mode,
                    snapshot_json,
                    recorded_at,
                ),
            )
            connection.executemany(
                "INSERT INTO graph_assets(run_id, product_identity, asset_json) VALUES (?, ?, ?)",
                ((snapshot.run_id, item.asset.product_identity.value, canonical_json(item)) for item in snapshot.assets),
            )
            connection.executemany(
                "INSERT INTO graph_attributes(run_id, product_identity, attribute_name, attribute_json) VALUES (?, ?, ?, ?)",
                ((snapshot.run_id, item.asset_identity.value, item.name, canonical_json(item)) for item in snapshot.attributes),
            )
            connection.executemany(
                "INSERT INTO graph_relationships(run_id, relationship_id, relationship_json) VALUES (?, ?, ?)",
                ((snapshot.run_id, item.relationship_id, canonical_json(item)) for item in snapshot.relationships),
            )
            connection.executemany(
                "INSERT INTO graph_provenance(run_id, sequence, provenance_json) VALUES (?, ?, ?)",
                ((snapshot.run_id, index, canonical_json(item)) for index, item in enumerate(snapshot.provenance)),
            )


__all__ = ["GraphContextMismatchError", "GraphSnapshotNotFoundError", "ModelGraphStore"]
