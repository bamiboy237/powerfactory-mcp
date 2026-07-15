from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from powerfactory_agent.domain import (
    CalculationInputDigest,
    CalculationRun,
    CommandKind,
    CommandSelector,
    ConfigurationKey,
    ConvergenceState,
    EvaluationStatus,
    ExtractionProvenance,
    GraphAsset,
    GraphDataOrigin,
    GraphRelationshipKind,
    GraphSnapshot,
    MetricDefinition,
    MetricKind,
    Quantity,
    ResultCellStatus,
    ResultMetric,
    ResultSnapshot,
    VersionedName,
)
from powerfactory_agent.gateway import DeterministicPrimitiveGateway
from powerfactory_agent.operations import (
    CalculationOverlayBindingError,
    PersistentModelGraph,
    evaluate_metric,
)
from powerfactory_agent.persistence import CalculationStore, SQLiteDatabase
from tests.unit.domain.fixtures import CONTENT_DIGEST, all_primary_models


class CalculationGraphOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = CalculationStore(SQLiteDatabase(Path(self.directory.name) / "overlays.sqlite"))
        self.gateway = DeterministicPrimitiveGateway()
        self.context = all_primary_models()[0]
        self.definition = MetricDefinition(
            "loading-fixture",
            self.context.assets[0].product_identity,
            MetricKind.EQUIPMENT_LOADING,
            self.gateway.load,
            self.gateway.equipment_loading_result,
            "%",
            None,
            Quantity("100", "%"),
            Quantity("10", "%"),
            Quantity("0.1", "%"),
            Quantity("1", "%"),
            "fixture limits/v1",
        )
        self.snapshot = self._calculation_snapshot()
        self.run = CalculationRun(
            self.snapshot.run_id,
            self.context.model_context_id,
            self.context.configuration_key,
            self.context.extraction_revision,
            self.snapshot.calculation_input_digest,
            CommandSelector(CommandKind.LOAD_FLOW, self.gateway.load_flow_command.contract),
            (),
            self.snapshot.policy,
            ConvergenceState.CONVERGED,
            "fixture-execution",
            (),
            (),
            self.snapshot.snapshot_id,
            self.snapshot.captured_at,
            self.snapshot.captured_at,
        )
        self.store.record(self.run, self.snapshot)
        self.graph_snapshot = GraphSnapshot(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4",
            self.context,
            CONTENT_DIGEST,
            (GraphAsset(self.context.assets[0], None, None, True, False),),
            (),
            (),
            (ExtractionProvenance("fixture", "graph extraction", GraphDataOrigin.EXTRACTED),),
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_restart_rebuild_binds_typed_result_and_violation_nodes_without_graph_write(self) -> None:
        restarted = CalculationStore(SQLiteDatabase(self.store.database.path))
        overlays = restarted.rebuild_overlays(self.snapshot.snapshot_id)
        projection = PersistentModelGraph.projection_with_calculation_overlays(
            self.graph_snapshot, restarted.snapshot(self.snapshot.snapshot_id), overlays
        )
        source = self.context.assets[0].product_identity.value
        self.assertEqual(3, projection.number_of_nodes())
        edge_kinds = {
            data["relationship_kind"]
            for _left, _right, _key, data in projection.edges(source, keys=True, data=True)
        }
        self.assertEqual(
            {GraphRelationshipKind.HAS_RESULT, GraphRelationshipKind.HAS_VIOLATION}, edge_kinds
        )
        self.assertEqual((), self.graph_snapshot.relationships)
        self.assertTrue(all("calculation_overlay" in projection.nodes[item.overlay_id] for item in overlays))

    def test_context_configuration_or_revision_mismatch_is_rejected_before_projection(self) -> None:
        mismatched = replace(
            self.snapshot,
            configuration_key=ConfigurationKey("configuration-key:v1:sha256:" + "f" * 64),
        )
        with self.assertRaises(CalculationOverlayBindingError):
            PersistentModelGraph.projection_with_calculation_overlays(
                self.graph_snapshot,
                mismatched,
                self.store.rebuild_overlays(self.snapshot.snapshot_id),
            )

    def _calculation_snapshot(self) -> ResultSnapshot:
        captured_at = datetime.now(timezone.utc)
        metric = ResultMetric(
            self.definition,
            ResultCellStatus.AVAILABLE,
            "111",
            "%",
            Quantity("111", "%"),
            None,
        )
        return ResultSnapshot(
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb4",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3",
            self.context.model_context_id,
            self.context.configuration_key,
            self.context.extraction_revision,
            CalculationInputDigest("calculation-input:v1:sha256:" + "7" * 64),
            VersionedName("load-flow-and-violation-policy", "v1"),
            (metric,),
            (evaluate_metric(metric),),
            captured_at,
        )


if __name__ == "__main__":
    unittest.main()
