from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from powerfactory_agent.domain import (
    AssetKind,
    AreaZoneQuery,
    ComponentsQuery,
    ConfigurationKey,
    ContentDigest,
    ElectricalPathQuery,
    ExtractionProvenance,
    ExtractionRevision,
    GraphAsset,
    GraphDataOrigin,
    GraphIncrementalRefresh,
    GraphQuery,
    GraphQueryKind,
    GraphRelationship,
    GraphRelationshipKind,
    GraphSnapshot,
    ImpactQuery,
    NeighborhoodQuery,
    ProductIdentity,
    TopologyDiffQuery,
)
from powerfactory_agent.operations import GraphQueryError, PersistentModelGraph
from powerfactory_agent.persistence import GraphContextMismatchError, ModelGraphStore, SQLiteDatabase
from powerfactory_agent.serialization import canonical_json, from_json
from tests.unit.domain.fixtures import CONTENT_DIGEST, all_primary_models, asset


class PersistentModelGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.temp.name) / "model-graph.sqlite")
        self.store = ModelGraphStore(self.database)
        self.graph = PersistentModelGraph(self.store)
        self.snapshot = self._snapshot(counter=1)
        self.graph.full_refresh(self.snapshot)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_restart_restore_and_sqlite_only_projection_rebuild_are_equivalent(self) -> None:
        restarted = PersistentModelGraph(ModelGraphStore(SQLiteDatabase(self.database.path)))
        restored = restarted.store.latest(
            expected_configuration_key=self.snapshot.context.configuration_key.value,
        )
        before = PersistentModelGraph.projection(self.snapshot)
        after = PersistentModelGraph.projection(restored)
        self.assertEqual(canonical_json(self.snapshot), canonical_json(restored))
        self.assertEqual(sorted(before.nodes), sorted(after.nodes))
        self.assertEqual(sorted(before.edges(keys=True)), sorted(after.edges(keys=True)))

    def test_context_mismatch_is_detected_before_cached_graph_use(self) -> None:
        with self.assertRaises(GraphContextMismatchError):
            self.store.latest(expected_configuration_key="configuration-key:v1:sha256:" + "f" * 64)

    def test_parallel_equipment_and_transformer_terminal_representation_are_preserved(self) -> None:
        projection = PersistentModelGraph.projection(self.snapshot)
        bus_1, bus_2 = self.snapshot.assets[0].asset.product_identity.value, self.snapshot.assets[1].asset.product_identity.value
        transformer = self.snapshot.assets[3].asset.product_identity.value
        self.assertEqual(2, projection.number_of_edges(bus_1, bus_2))
        self.assertEqual({"branch-a", "branch-b"}, set(projection[bus_1][bus_2]))
        self.assertEqual(2, self.snapshot.assets[3].transformer_winding_count)
        self.assertEqual(2, projection.degree(transformer))

    def test_queries_are_bounded_and_return_stable_references(self) -> None:
        context = self.snapshot.context
        query = GraphQuery(GraphQueryKind.NEIGHBORHOOD, context.model_context_id, context.extraction_revision, limit=2)
        result = self.graph.neighborhood(NeighborhoodQuery(query, self.snapshot.assets[0].asset.product_identity, 3))
        self.assertLessEqual(len(result.asset_references), 2)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.relationship_ids), 2)

        path_query = GraphQuery(GraphQueryKind.ELECTRICAL_PATH, context.model_context_id, context.extraction_revision, limit=10)
        path = self.graph.electrical_path(ElectricalPathQuery(path_query, self.snapshot.assets[0].asset.product_identity, self.snapshot.assets[2].asset.product_identity))
        self.assertGreaterEqual(path.total_matches, 3)
        area_query = GraphQuery(GraphQueryKind.AREA_OR_ZONE, context.model_context_id, context.extraction_revision, limit=10)
        self.assertEqual(2, self.graph.assets_in_area_or_zone(AreaZoneQuery(area_query, None, "north")).total_matches)
        component_query = GraphQuery(GraphQueryKind.COMPONENTS, context.model_context_id, context.extraction_revision, limit=10)
        self.assertEqual(4, self.graph.connected_components(ComponentsQuery(component_query)).total_matches)
        impact_query = GraphQuery(GraphQueryKind.IMPACT, context.model_context_id, context.extraction_revision, limit=10)
        self.assertGreaterEqual(self.graph.impact(ImpactQuery(impact_query, self.snapshot.assets[0].asset.product_identity)).total_matches, 1)

    def test_known_asset_incremental_refresh_and_topology_diff(self) -> None:
        next_context = replace(
            self.snapshot.context,
            extraction_revision=ExtractionRevision(self.snapshot.context.model_context_id, 2),
        )
        changed_transformer = replace(self.snapshot.assets[3], in_service=False)
        changed_relationship = replace(self.snapshot.relationships[-1], in_service=False)
        refreshed = self.graph.incremental_refresh(
            GraphIncrementalRefresh(
                run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
                context=next_context,
                extraction_fingerprint=CONTENT_DIGEST,
                changed_assets=(changed_transformer,),
                changed_attributes=(),
                changed_relationships=(changed_relationship,),
                removed_relationship_ids=(),
                provenance=(ExtractionProvenance("fixture", "known transformer refresh", GraphDataOrigin.EXTRACTED),),
            )
        )
        self.assertFalse(next(item for item in refreshed.assets if item.asset.asset_kind is AssetKind.TRANSFORMER).in_service)
        query = GraphQuery(GraphQueryKind.TOPOLOGY_DIFF, next_context.model_context_id, next_context.extraction_revision, limit=10)
        diff = self.graph.topology_diff(TopologyDiffQuery(query, self.snapshot.context.extraction_revision))
        self.assertIn(changed_transformer.asset, diff.asset_references)
        self.assertIn(changed_relationship.relationship_id, diff.relationship_ids)

    def test_graph_dtos_are_strictly_serializable(self) -> None:
        self.assertEqual(self.snapshot, from_json(GraphSnapshot, canonical_json(self.snapshot)))

    def test_topology_diff_rejects_revisions_from_different_configurations(self) -> None:
        changed_configuration = ConfigurationKey("configuration-key:v1:sha256:" + "f" * 64)
        changed_context = replace(
            self.snapshot.context,
            configuration_key=changed_configuration,
            freshness=replace(self.snapshot.context.freshness, configuration_key=changed_configuration),
            extraction_revision=ExtractionRevision(self.snapshot.context.model_context_id, 3),
        )
        changed_snapshot = replace(
            self.snapshot,
            run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3",
            context=changed_context,
        )
        self.graph.full_refresh(changed_snapshot)
        query = GraphQuery(GraphQueryKind.TOPOLOGY_DIFF, changed_context.model_context_id, changed_context.extraction_revision, limit=10)
        with self.assertRaises(GraphQueryError):
            self.graph.topology_diff(TopologyDiffQuery(query, self.snapshot.context.extraction_revision))

    @staticmethod
    def _snapshot(counter: int) -> GraphSnapshot:
        base_context = all_primary_models()[0]
        assert hasattr(base_context, "assets")
        identifiers = (
            "10000000-0000-4000-8000-000000000001",
            "20000000-0000-4000-8000-000000000002",
            "30000000-0000-4000-8000-000000000003",
            "40000000-0000-4000-8000-000000000004",
        )
        kinds = (AssetKind.BUS, AssetKind.BUS, AssetKind.BUS, AssetKind.TRANSFORMER)
        assets = tuple(
            replace(
                asset(),
                product_identity=ProductIdentity(identity),
                display_name=f"Asset {index}",
                asset_kind=kind,
                locator=replace(asset().locator, object_class="ElmTr2" if kind is AssetKind.TRANSFORMER else "ElmTerm"),
            )
            for index, (identity, kind) in enumerate(zip(identifiers, kinds), start=1)
        )
        context = replace(
            base_context,
            assets=assets,
            extraction_revision=ExtractionRevision(base_context.model_context_id, counter),
        )
        graph_assets = (
            GraphAsset(assets[0], None, "north", True, False),
            GraphAsset(assets[1], None, "north", True, False),
            GraphAsset(assets[2], None, "south", True, False),
            GraphAsset(assets[3], None, None, True, False, transformer_winding_count=2),
        )
        relationships = (
            GraphRelationship("branch-a", assets[0].product_identity, assets[1].product_identity, GraphRelationshipKind.CONNECTS, GraphDataOrigin.EXTRACTED, True),
            GraphRelationship("branch-b", assets[0].product_identity, assets[1].product_identity, GraphRelationshipKind.CONNECTS, GraphDataOrigin.EXTRACTED, True),
            GraphRelationship("transformer-a", assets[1].product_identity, assets[3].product_identity, GraphRelationshipKind.TERMINAL_OF, GraphDataOrigin.EXTRACTED, True),
            GraphRelationship("transformer-b", assets[2].product_identity, assets[3].product_identity, GraphRelationshipKind.TERMINAL_OF, GraphDataOrigin.EXTRACTED, True),
        )
        return GraphSnapshot(
            run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
            context=context,
            extraction_fingerprint=CONTENT_DIGEST,
            assets=graph_assets,
            attributes=(),
            relationships=relationships,
            provenance=(ExtractionProvenance("fixture", "full extraction", GraphDataOrigin.EXTRACTED),),
        )
