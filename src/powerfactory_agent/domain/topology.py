"""Typed, bounded contracts for the persistent power-system graph.

SQLite retains source records.  A NetworkX ``MultiGraph`` is rebuilt from those
records for every projection so parallel equipment cannot disappear through
edge coalescing.  Transformers are represented as equipment nodes connected to
their two or three terminal nodes; that uniform representation preserves all
winding evidence without inventing a hyperedge format.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from .models import AssetReference, ModelContext
from .values import (
    AssetKind,
    MAX_COLLECTION_LENGTH,
    MAX_PAGE_SIZE,
    ContentDigest,
    ExtractionRevision,
    ProductIdentity,
    require_collection,
    require_text,
    require_uuid4,
)


MAX_GRAPH_HOPS = 12
MAX_GRAPH_RESULTS = MAX_PAGE_SIZE


def _bounded(value: int, field: str, *, minimum: int = 0, maximum: int = MAX_GRAPH_RESULTS) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")


class GraphDataOrigin(str, Enum):
    EXTRACTED = "extracted"
    DERIVED = "derived"
    INFERRED = "inferred"


class GraphRelationshipKind(str, Enum):
    CONNECTS = "connects"
    CONTAINS = "contains"
    MEMBER_OF = "member_of"
    TERMINAL_OF = "terminal_of"


class GraphQueryKind(str, Enum):
    NEIGHBORHOOD = "neighborhood"
    ELECTRICAL_PATH = "electrical_path"
    AREA_OR_ZONE = "area_or_zone"
    COMPONENTS = "components"
    IMPACT = "impact"
    TOPOLOGY_DIFF = "topology_diff"


@dataclass(frozen=True, slots=True)
class GraphAsset:
    asset: AssetReference
    area_identity: Optional[ProductIdentity]
    zone_key: Optional[str]
    in_service: bool
    is_switch: bool
    switch_closed: Optional[bool] = None
    transformer_winding_count: int = 0

    def __post_init__(self) -> None:
        if self.zone_key is not None:
            require_text(self.zone_key, "GraphAsset.zone_key", maximum=256)
        _bounded(self.transformer_winding_count, "GraphAsset.transformer_winding_count", maximum=3)
        if self.transformer_winding_count not in (0, 2, 3):
            raise ValueError("transformer winding count must be zero, two, or three")
        if self.is_switch != (self.switch_closed is not None):
            raise ValueError("switch state is required only for switch assets")
        if self.transformer_winding_count and self.asset.asset_kind is not AssetKind.TRANSFORMER:
            raise ValueError("only transformer assets may declare winding count")


@dataclass(frozen=True, slots=True)
class GraphAttribute:
    asset_identity: ProductIdentity
    name: str
    value: str
    origin: GraphDataOrigin

    def __post_init__(self) -> None:
        require_text(self.name, "GraphAttribute.name", maximum=256)
        require_text(self.value, "GraphAttribute.value", maximum=2048)


@dataclass(frozen=True, slots=True)
class GraphRelationship:
    relationship_id: str
    source_identity: ProductIdentity
    target_identity: ProductIdentity
    kind: GraphRelationshipKind
    origin: GraphDataOrigin
    in_service: bool

    def __post_init__(self) -> None:
        require_text(self.relationship_id, "GraphRelationship.relationship_id", maximum=256)
        if self.source_identity == self.target_identity:
            raise ValueError("graph relationships may not self-reference")


@dataclass(frozen=True, slots=True)
class ExtractionProvenance:
    source: str
    detail: str
    origin: GraphDataOrigin

    def __post_init__(self) -> None:
        require_text(self.source, "ExtractionProvenance.source", maximum=256)
        require_text(self.detail, "ExtractionProvenance.detail", maximum=2048)


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    run_id: str
    context: ModelContext
    extraction_fingerprint: ContentDigest
    assets: Tuple[GraphAsset, ...]
    attributes: Tuple[GraphAttribute, ...]
    relationships: Tuple[GraphRelationship, ...]
    provenance: Tuple[ExtractionProvenance, ...]

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "GraphSnapshot.run_id")
        for field in ("assets", "attributes", "relationships", "provenance"):
            require_collection(getattr(self, field), f"GraphSnapshot.{field}")
        identities = tuple(item.asset.product_identity.value for item in self.assets)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
            raise ValueError("graph assets require deterministic unique product identities")
        context_identities = tuple(asset.product_identity.value for asset in self.context.assets)
        if len(set(context_identities)) != len(context_identities) or set(identities) != set(context_identities):
            raise ValueError("graph snapshot assets must exactly match its context assets")
        known = set(identities)
        if any(item.asset_identity.value not in known for item in self.attributes):
            raise ValueError("graph attribute references an unknown asset")
        relationship_ids = tuple(item.relationship_id for item in self.relationships)
        if relationship_ids != tuple(sorted(relationship_ids)) or len(set(relationship_ids)) != len(relationship_ids):
            raise ValueError("graph relationships require deterministic unique ids")
        if any(
            item.source_identity.value not in known or item.target_identity.value not in known
            for item in self.relationships
        ):
            raise ValueError("graph relationship references an unknown asset")


@dataclass(frozen=True, slots=True)
class GraphIncrementalRefresh:
    run_id: str
    context: ModelContext
    extraction_fingerprint: ContentDigest
    changed_assets: Tuple[GraphAsset, ...]
    changed_attributes: Tuple[GraphAttribute, ...]
    changed_relationships: Tuple[GraphRelationship, ...]
    removed_relationship_ids: Tuple[str, ...]
    provenance: Tuple[ExtractionProvenance, ...]

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "GraphIncrementalRefresh.run_id")
        for field in ("changed_assets", "changed_attributes", "changed_relationships", "removed_relationship_ids", "provenance"):
            require_collection(getattr(self, field), f"GraphIncrementalRefresh.{field}")
        known = {asset.product_identity.value for asset in self.context.assets}
        changed = tuple(asset.asset.product_identity.value for asset in self.changed_assets)
        if len(set(changed)) != len(changed) or any(identity not in known for identity in changed):
            raise ValueError("incremental refresh may change only known, unique assets")
        if any(item.asset_identity.value not in known for item in self.changed_attributes):
            raise ValueError("incremental attribute references unknown context asset")
        for relationship_id in self.removed_relationship_ids:
            require_text(relationship_id, "GraphIncrementalRefresh.removed_relationship_ids entry", maximum=256)


@dataclass(frozen=True, slots=True)
class GraphQuery:
    kind: GraphQueryKind
    model_context_id: str
    extraction_revision: ExtractionRevision
    limit: int = 25

    def __post_init__(self) -> None:
        require_uuid4(self.model_context_id, "GraphQuery.model_context_id")
        if self.extraction_revision.scope_id != self.model_context_id:
            raise ValueError("query revision must belong to query context")
        _bounded(self.limit, "GraphQuery.limit", minimum=1)


@dataclass(frozen=True, slots=True)
class NeighborhoodQuery:
    query: GraphQuery
    center_identity: ProductIdentity
    hops: int

    def __post_init__(self) -> None:
        if self.query.kind is not GraphQueryKind.NEIGHBORHOOD:
            raise ValueError("neighborhood query kind is required")
        _bounded(self.hops, "NeighborhoodQuery.hops", minimum=1, maximum=MAX_GRAPH_HOPS)


@dataclass(frozen=True, slots=True)
class ElectricalPathQuery:
    query: GraphQuery
    source_identity: ProductIdentity
    target_identity: ProductIdentity

    def __post_init__(self) -> None:
        if self.query.kind is not GraphQueryKind.ELECTRICAL_PATH:
            raise ValueError("electrical path query kind is required")
        if self.source_identity == self.target_identity:
            raise ValueError("electrical path endpoints must differ")


@dataclass(frozen=True, slots=True)
class AreaZoneQuery:
    query: GraphQuery
    area_identity: Optional[ProductIdentity]
    zone_key: Optional[str]

    def __post_init__(self) -> None:
        if self.query.kind is not GraphQueryKind.AREA_OR_ZONE:
            raise ValueError("area/zone query kind is required")
        if (self.area_identity is None) == (self.zone_key is None):
            raise ValueError("exactly one area identity or zone key is required")
        if self.zone_key is not None:
            require_text(self.zone_key, "AreaZoneQuery.zone_key", maximum=256)


@dataclass(frozen=True, slots=True)
class ComponentsQuery:
    query: GraphQuery

    def __post_init__(self) -> None:
        if self.query.kind is not GraphQueryKind.COMPONENTS:
            raise ValueError("components query kind is required")


@dataclass(frozen=True, slots=True)
class ImpactQuery:
    query: GraphQuery
    changed_identity: ProductIdentity
    hops: int = 1

    def __post_init__(self) -> None:
        if self.query.kind is not GraphQueryKind.IMPACT:
            raise ValueError("impact query kind is required")
        _bounded(self.hops, "ImpactQuery.hops", minimum=1, maximum=MAX_GRAPH_HOPS)


@dataclass(frozen=True, slots=True)
class TopologyDiffQuery:
    query: GraphQuery
    previous_revision: ExtractionRevision

    def __post_init__(self) -> None:
        if self.query.kind is not GraphQueryKind.TOPOLOGY_DIFF:
            raise ValueError("topology diff query kind is required")
        if self.previous_revision.scope_id != self.query.model_context_id:
            raise ValueError("topology diff revisions must share a model context")


@dataclass(frozen=True, slots=True)
class GraphQueryResult:
    query: GraphQuery
    asset_references: Tuple[AssetReference, ...]
    relationship_ids: Tuple[str, ...]
    total_matches: int
    truncated: bool
    summary: str

    def __post_init__(self) -> None:
        require_collection(self.asset_references, "GraphQueryResult.asset_references")
        require_collection(self.relationship_ids, "GraphQueryResult.relationship_ids")
        if len(self.asset_references) > self.query.limit or len(self.relationship_ids) > self.query.limit:
            raise ValueError("graph query results must not exceed the requested limit")
        identities = tuple(item.product_identity.value for item in self.asset_references)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
            raise ValueError("graph query asset references must be unique and ordered")
        if tuple(self.relationship_ids) != tuple(sorted(self.relationship_ids)) or len(set(self.relationship_ids)) != len(self.relationship_ids):
            raise ValueError("graph query relationships must be unique and ordered")
        _bounded(self.total_matches, "GraphQueryResult.total_matches", maximum=MAX_COLLECTION_LENGTH)
        if self.total_matches < len(self.asset_references):
            raise ValueError("total matches cannot be smaller than returned asset references")
        require_text(self.summary, "GraphQueryResult.summary", maximum=512)


__all__ = [name for name in globals() if not name.startswith("_")]
