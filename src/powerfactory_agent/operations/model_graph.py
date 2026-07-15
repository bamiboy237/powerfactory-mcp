"""Read-only, bounded graph operations over a rebuildable NetworkX projection."""

from __future__ import annotations

import networkx as nx

from powerfactory_agent.domain import AssetReference
from powerfactory_agent.domain.topology import (
    AreaZoneQuery,
    ComponentsQuery,
    ElectricalPathQuery,
    GraphIncrementalRefresh,
    GraphQuery,
    GraphQueryResult,
    GraphSnapshot,
    ImpactQuery,
    NeighborhoodQuery,
    TopologyDiffQuery,
)
from powerfactory_agent.persistence.model_graph_store import ModelGraphStore


class GraphQueryError(LookupError):
    pass


class PersistentModelGraph:
    """Owns no vendor handle; all queryable state comes from persisted DTOs."""

    def __init__(self, store: ModelGraphStore) -> None:
        self.store = store

    def full_refresh(self, snapshot: GraphSnapshot) -> GraphSnapshot:
        return self.store.record_full(snapshot)

    def incremental_refresh(self, refresh: GraphIncrementalRefresh) -> GraphSnapshot:
        return self.store.record_incremental(refresh)

    def neighborhood(self, request: NeighborhoodQuery) -> GraphQueryResult:
        snapshot, graph = self._projection_for(request.query)
        center = request.center_identity.value
        self._require_node(graph, center)
        distances = nx.single_source_shortest_path_length(graph, center, cutoff=request.hops)
        identities = tuple(sorted(distances))
        relationship_ids = self._relationships_for_nodes(graph, identities)
        return self._result(request.query, snapshot, identities, relationship_ids, "neighborhood")

    def electrical_path(self, request: ElectricalPathQuery) -> GraphQueryResult:
        snapshot, graph = self._projection_for(request.query)
        source, target = request.source_identity.value, request.target_identity.value
        self._require_node(graph, source)
        self._require_node(graph, target)
        energized = nx.MultiGraph(
            (left, right, key, data)
            for left, right, key, data in graph.edges(keys=True, data=True)
            if data["relationship"].in_service
            and graph.nodes[left]["graph_asset"].in_service
            and graph.nodes[right]["graph_asset"].in_service
            and _energized_asset(graph.nodes[left]["graph_asset"])
            and _energized_asset(graph.nodes[right]["graph_asset"])
        )
        energized.add_nodes_from(
            (node, data)
            for node, data in graph.nodes(data=True)
            if _energized_asset(data["graph_asset"])
        )
        try:
            identities = tuple(nx.shortest_path(energized, source, target))
        except nx.NetworkXNoPath as exc:
            raise GraphQueryError("no in-service electrical path exists") from exc
        return self._result(
            request.query,
            snapshot,
            identities,
            self._relationships_for_nodes(graph, identities),
            "electrical path",
        )

    def assets_in_area_or_zone(self, request: AreaZoneQuery) -> GraphQueryResult:
        snapshot, graph = self._projection_for(request.query)
        identities = tuple(
            sorted(
                node
                for node, data in graph.nodes(data=True)
                if (
                    request.area_identity is not None
                    and data["graph_asset"].area_identity == request.area_identity
                )
                or (request.zone_key is not None and data["graph_asset"].zone_key == request.zone_key)
            )
        )
        return self._result(request.query, snapshot, identities, self._relationships_for_nodes(graph, identities), "area/zone assets")

    def connected_components(self, request: ComponentsQuery) -> GraphQueryResult:
        snapshot, graph = self._projection_for(request.query)
        in_service = nx.MultiGraph(
            (left, right, key, data)
            for left, right, key, data in graph.edges(keys=True, data=True)
            if data["relationship"].in_service
        )
        in_service.add_nodes_from(graph.nodes)
        components = sorted((tuple(sorted(component)) for component in nx.connected_components(in_service)), key=lambda item: (-len(item), item))
        identities = tuple(identity for component in components for identity in component)
        return self._result(request.query, snapshot, identities, self._relationships_for_nodes(graph, identities), f"{len(components)} connected components")

    def impact(self, request: ImpactQuery) -> GraphQueryResult:
        snapshot, graph = self._projection_for(request.query)
        changed = request.changed_identity.value
        self._require_node(graph, changed)
        affected_graph = graph.copy()
        affected_graph.remove_node(changed)
        nearby = nx.single_source_shortest_path_length(graph, changed, cutoff=request.hops)
        identities = tuple(sorted(identity for identity in nearby if identity != changed and identity in affected_graph))
        return self._result(request.query, snapshot, identities, self._relationships_for_nodes(graph, identities), "outage/change impact")

    def topology_diff(self, request: TopologyDiffQuery) -> GraphQueryResult:
        current, graph = self._projection_for(request.query)
        previous = self.store.revision(
            context_id=request.query.model_context_id,
            counter=request.previous_revision.counter,
        )
        if previous.context.configuration_key != current.context.configuration_key:
            raise GraphQueryError("topology diff revisions do not share a verified configuration")
        previous_assets = {asset.asset.product_identity.value for asset in previous.assets}
        current_assets = {asset.asset.product_identity.value for asset in current.assets}
        changed_assets = current_assets.symmetric_difference(previous_assets)
        previous_relationships = {item.relationship_id: item for item in previous.relationships}
        current_relationships = {item.relationship_id: item for item in current.relationships}
        changed_relationships = set(previous_relationships).symmetric_difference(current_relationships)
        for relationship_id in set(previous_relationships).intersection(current_relationships):
            if previous_relationships[relationship_id] != current_relationships[relationship_id]:
                changed_relationships.add(relationship_id)
        for relationship_id in changed_relationships:
            relationship = current_relationships.get(relationship_id) or previous_relationships[relationship_id]
            changed_assets.add(relationship.source_identity.value)
            changed_assets.add(relationship.target_identity.value)
        return self._result(request.query, current, tuple(sorted(changed_assets)), tuple(sorted(changed_relationships)), "topology diff")

    @staticmethod
    def projection(snapshot: GraphSnapshot) -> nx.MultiGraph:
        """Build a fresh projection from records, preserving every relationship key."""
        graph = nx.MultiGraph()
        for graph_asset in snapshot.assets:
            graph.add_node(graph_asset.asset.product_identity.value, graph_asset=graph_asset)
        for relationship in snapshot.relationships:
            graph.add_edge(
                relationship.source_identity.value,
                relationship.target_identity.value,
                key=relationship.relationship_id,
                relationship=relationship,
            )
        return graph

    def _projection_for(self, query: GraphQuery) -> tuple[GraphSnapshot, nx.MultiGraph]:
        snapshot = self.store.revision(
            context_id=query.model_context_id,
            counter=query.extraction_revision.counter,
        )
        if snapshot.context.extraction_revision != query.extraction_revision:
            raise GraphQueryError("persisted extraction revision does not match query")
        return snapshot, self.projection(snapshot)

    @staticmethod
    def _require_node(graph: nx.MultiGraph, identity: str) -> None:
        if identity not in graph:
            raise GraphQueryError("asset identity is not present in the extraction")

    @staticmethod
    def _relationships_for_nodes(graph: nx.MultiGraph, identities: tuple[str, ...]) -> tuple[str, ...]:
        selected = set(identities)
        return tuple(
            sorted(
                data["relationship"].relationship_id
                for left, right, _key, data in graph.edges(keys=True, data=True)
                if left in selected and right in selected
            )
        )

    @staticmethod
    def _result(
        query: GraphQuery,
        snapshot: GraphSnapshot,
        identities: tuple[str, ...],
        relationship_ids: tuple[str, ...],
        label: str,
    ) -> GraphQueryResult:
        references = {item.asset.product_identity.value: item.asset for item in snapshot.assets}
        visible_identities = tuple(sorted(identities))[: query.limit]
        visible_relationships = tuple(sorted(relationship_ids))[: query.limit]
        total = len(identities)
        return GraphQueryResult(
            query=query,
            asset_references=tuple(references[identity] for identity in visible_identities if identity in references),
            relationship_ids=visible_relationships,
            total_matches=total,
            truncated=total > query.limit or len(relationship_ids) > query.limit,
            summary=f"{label}: {total} matching assets; {len(relationship_ids)} matching relationships",
        )


__all__ = ["GraphQueryError", "PersistentModelGraph"]


def _energized_asset(graph_asset: object) -> bool:
    """Out-of-service and open switches break electrical paths but stay observable."""
    return bool(
        getattr(graph_asset, "in_service")
        and (not getattr(graph_asset, "is_switch") or getattr(graph_asset, "switch_closed"))
    )
