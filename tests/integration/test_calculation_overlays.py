from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from powerfactory_agent.domain import (
    CommandKind,
    CommandSelector,
    ConvergenceState,
    EvaluationStatus,
    FindingTrend,
    LoadFlowRequest,
    MetricDefinition,
    MetricKind,
    Quantity,
    ResultCellStatus,
    ResultMetric,
    ResultSnapshot,
    VersionedName,
)
from powerfactory_agent.gateway import DeterministicPrimitiveGateway, SerializedPowerFactoryOwner
from powerfactory_agent.operations import LoadFlowService, compare_result_snapshots, evaluate_metric
from powerfactory_agent.persistence import CalculationStore, OperationStore, SQLiteDatabase
from powerfactory_agent.serialization import canonical_json, from_json
from powerfactory_agent.domain.values import CalculationInputDigest, ExtractionRevision, ProductIdentity


POLICY = VersionedName("load-flow-and-violation-policy", "v1")


class AuditedGateway(DeterministicPrimitiveGateway):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def execute_command(self, request):
        self.calls.append("execute_command")
        return super().execute_command(request)

    def collect_results(self, request):
        self.calls.append("collect_results")
        return super().collect_results(request)

    def read_logs(self, request):
        self.calls.append("read_logs")
        return super().read_logs(request)


class CalculationOverlayIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        database = SQLiteDatabase(Path(self.directory.name) / "calculation.sqlite")
        self.gateway = AuditedGateway()
        self.owner = SerializedPowerFactoryOwner(
            self.gateway,
            OperationStore(database),
            max_queue_size=32,
            queue_deadline_ms=2_000,
            client_response_deadline_ms=2_000,
            engine_health_threshold_ms=5_000,
            watchdog_interval_ms=2,
        )
        self.store = CalculationStore(database)
        self.service = LoadFlowService(self.owner, self.store)
        self._start_context()

    def tearDown(self) -> None:
        self.owner.shutdown_serialization(timeout_ms=1_000)
        self.directory.cleanup()

    def test_converged_run_is_owner_only_persisted_and_rebuilds_overlays_after_restart(self) -> None:
        request = self._request("calculation-1")
        run = self.service.run_validated_load_flow(request)
        self.assertEqual(ConvergenceState.CONVERGED, run.convergence_state)
        self.assertIsNotNone(run.result_snapshot_id)
        self.assertEqual({"execute_command", "collect_results", "read_logs"}, set(self.gateway.calls))
        snapshot = self.store.snapshot(run.result_snapshot_id)
        self.assertEqual(run, self.service.get_calculation_run(run.run_id))
        self.assertEqual((), self.service.find_violations(run.result_snapshot_id))
        self.assertEqual(("loading-north", "voltage-north"), tuple(item.definition.definition_id for item in snapshot.metrics))
        self.assertEqual((EvaluationStatus.SAFE, EvaluationStatus.SAFE), tuple(item.status for item in snapshot.evaluations))
        restarted = CalculationStore(SQLiteDatabase(self.store.database.path))
        self.assertEqual(canonical_json(snapshot), canonical_json(restarted.snapshot(run.result_snapshot_id)))
        self.assertEqual(restarted.overlays(run.result_snapshot_id), restarted.rebuild_overlays(run.result_snapshot_id))
        self.assertTrue(all(item.snapshot_id == run.result_snapshot_id for item in restarted.rebuild_overlays(run.result_snapshot_id)))
        self.assertEqual(snapshot, from_json(ResultSnapshot, canonical_json(snapshot)))

    def test_nonzero_command_code_persists_not_converged_without_snapshot_or_result_classification(self) -> None:
        request = replace(
            self._request("calculation-nonconverged"),
            command=CommandSelector(CommandKind.RMS_SIMULATION, self.gateway.load_flow_command.contract),
        )
        run = self.service.run_validated_load_flow(request)
        self.assertEqual(ConvergenceState.NOT_CONVERGED, run.convergence_state)
        self.assertIsNone(run.result_snapshot_id)
        self.assertEqual(run, self.store.run(run.run_id))
        self.assertNotIn("collect_results", self.gateway.calls)

    def test_comparison_distinguishes_equivalent_material_new_resolved_unchanged_and_not_evaluated(self) -> None:
        baseline = self._snapshot("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1", "0.94", "101")
        candidate = self._snapshot("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2", "0.94", "99")
        comparison = compare_result_snapshots(baseline, candidate)
        self.assertFalse(comparison.result_equivalent)
        self.assertTrue(any(item.material for item in comparison.comparisons))
        self.assertIn(Quantity("-2", "%"), {item.delta for item in comparison.comparisons})
        self.assertEqual({FindingTrend.UNCHANGED, FindingTrend.RESOLVED}, {item.trend for item in comparison.findings})
        identical = self._snapshot("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb4", "0.94", "101")
        self.assertTrue(compare_result_snapshots(baseline, identical).result_equivalent)
        new_finding = self._snapshot("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb5", "0.94", "99")
        new_baseline = self._snapshot("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa5", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb6", "0.95", "99")
        self.assertIn(FindingTrend.NEW, {item.trend for item in compare_result_snapshots(new_baseline, new_finding).findings})
        unavailable = replace(
            candidate,
            snapshot_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3",
            metrics=(
                ResultMetric(candidate.metrics[0].definition, ResultCellStatus.MISSING, None, None, None, "fixture"),
                candidate.metrics[1],
            ),
            evaluations=(
                evaluate_metric(ResultMetric(candidate.metrics[0].definition, ResultCellStatus.MISSING, None, None, None, "fixture")),
                candidate.evaluations[1],
            ),
        )
        unavailable_comparison = compare_result_snapshots(baseline, unavailable)
        self.assertIn(FindingTrend.NOT_EVALUATED, {item.trend for item in unavailable_comparison.findings})

    def _start_context(self) -> None:
        start = self.owner.submit_start(
            self._session_request(), idempotency_key="calculation-start"
        )
        self._await(start.operation_id, object)
        activation = self.owner.submit_activate_context(
            self._activation_request(), idempotency_key="calculation-context"
        )
        context = self._await(activation.operation_id, object).context
        self.configuration_key = context.configuration_key
        assert self.configuration_key is not None

    def _request(self, key: str) -> LoadFlowRequest:
        return LoadFlowRequest(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
            self.configuration_key,
            ExtractionRevision("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", 1),
            self.gateway.load_flow_command,
            (),
            (self._loading_definition(), self._voltage_definition()),
            POLICY,
            key,
        )

    def _snapshot(self, run_id: str, snapshot_id: str, voltage: str, loading: str) -> ResultSnapshot:
        request = self._request("comparison-fixture")
        digest = CalculationInputDigest("calculation-input:v1:sha256:" + "1" * 64)
        metrics = (
            ResultMetric(self._loading_definition(), ResultCellStatus.AVAILABLE, loading, "%", Quantity(loading, "%"), None),
            ResultMetric(self._voltage_definition(), ResultCellStatus.AVAILABLE, voltage, "p.u.", Quantity(voltage, "p.u."), None),
        )
        return ResultSnapshot(
            snapshot_id,
            run_id,
            request.context_id,
            request.configuration_key,
            request.extraction_revision,
            digest,
            POLICY,
            metrics,
            tuple(evaluate_metric(metric) for metric in metrics),
            datetime.now(timezone.utc),
        )

    def _voltage_definition(self) -> MetricDefinition:
        return MetricDefinition(
            "voltage-north", ProductIdentity("10000000-0000-4000-8000-000000000001"), MetricKind.BUS_VOLTAGE,
            self.gateway.bus, self.gateway.bus_voltage_result, "p.u.", Quantity("0.95", "p.u."), Quantity("1.05", "p.u."),
            Quantity("0.02", "p.u."), Quantity("0.001", "p.u."), Quantity("0.01", "p.u."), "fixture limits/v1",
        )

    def _loading_definition(self) -> MetricDefinition:
        return MetricDefinition(
            "loading-north", ProductIdentity("20000000-0000-4000-8000-000000000002"), MetricKind.EQUIPMENT_LOADING,
            self.gateway.load, self.gateway.equipment_loading_result, "%", None, Quantity("100", "%"), Quantity("10", "%"),
            Quantity("0.1", "%"), Quantity("1", "%"), "fixture limits/v1",
        )

    @staticmethod
    def _session_request():
        from powerfactory_agent.domain import SessionStartRequest
        return SessionStartRequest("fixture", "profile", "2026", "SP0", False)

    @staticmethod
    def _activation_request():
        from powerfactory_agent.domain import ContextActivationRequest
        return ContextActivationRequest("project-fixture", "study-fixture", None)

    def _await(self, operation_id, result_type):
        import time
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            record = self.owner.status(operation_id)
            if record.terminal:
                # The setup only needs the concrete result shape, so derive it from the handler.
                if record.handler_name.endswith("start"):
                    from powerfactory_agent.domain import SessionObservation
                    return self.owner.completed_result(operation_id, SessionObservation)
                from powerfactory_agent.domain import ContextActivationObservation
                return self.owner.completed_result(operation_id, ContextActivationObservation)
            time.sleep(0.002)
        self.fail("owner setup operation did not complete")


if __name__ == "__main__":
    unittest.main()
