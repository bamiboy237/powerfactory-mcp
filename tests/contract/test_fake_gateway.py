"""Contract tests for the portable deterministic gateway implementation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import unittest

from powerfactory_agent.domain import (
    AssetKind,
    AttributeQuantity,
    ChangePreview,
    CommandSetting,
    ConfigurationKey,
    ContentDigest,
    EngineeringOperation,
    ExecutionAuthorization,
    ExtractionRevision,
    InventoryQuery,
    MutationStrategy,
    NamedValue,
    OperationType,
    PageCursor,
    ProposedAssetChange,
    Quantity,
    VersionedName,
    WorkflowVersion,
    WorkspaceRevision,
)
from powerfactory_agent.gateway import (
    AuthorizationInvalid,
    AuthorizationRequired,
    CalculationNonConvergence,
    ConfigurationMismatch,
    CursorInvalid,
    CursorStale,
    DeterministicFakeGateway,
    DeterministicHeadlineHarness,
    GatewayErrorCategory,
    InvalidOperation,
    PartialMutation,
    RollbackConflictError,
    StaleContext,
)
from powerfactory_agent.serialization import canonical_digest, canonical_json


WORKSPACE_ID = "20000000-0000-4000-8000-000000000001"
WORKFLOW_ID = "30000000-0000-4000-8000-000000000001"


def _scaled_preview(
    gateway: DeterministicFakeGateway,
    *,
    scale: str,
    preview_id: str = "preview-1",
    workflow_version: int = 1,
) -> ChangePreview:
    context = gateway.active_context()
    loads = gateway.area_assets(
        context.configuration_key,
        gateway.area,
        asset_kind=AssetKind.LOAD,
        limit=10,
    )
    attributes = ("active_power", "reactive_power")
    reads = gateway.read_attributes(context.configuration_key, loads, attributes, limit=10)
    multiplier = Decimal(scale)
    changes = []
    for asset, values in reads:
        proposed = tuple(
            AttributeQuantity(
                item.attribute,
                Quantity(Decimal(str(item.value.value)) * multiplier, item.value.unit),
            )
            for item in values
        )
        changes.append(ProposedAssetChange(asset=asset, before=values, proposed=proposed))
    fingerprint = gateway.live_state_fingerprint(context.configuration_key, loads, attributes)
    digest = ContentDigest(
        canonical_digest(
            (
                preview_id,
                scale,
                tuple(
                    (
                        change.asset.product_identity.value,
                        tuple(
                            (item.attribute, str(item.value.value), item.value.unit)
                            for item in change.proposed
                        ),
                    )
                    for change in changes
                ),
                fingerprint.value,
            )
        )
    )
    now = context.extracted_at
    return ChangePreview(
        preview_id=preview_id,
        model_context_id=context.model_context_id,
        workspace_id=WORKSPACE_ID,
        workflow_id=WORKFLOW_ID,
        operation=EngineeringOperation(
            operation_type=OperationType.AREA_LOAD_SCALING,
            operation_specification=VersionedName("fake-area-load-scaling", "1.0.0"),
            parameters=(NamedValue("area_identity", gateway.area.product_identity.value),),
        ),
        resolved_changes=tuple(changes),
        selection_criteria=(NamedValue("area_identity", gateway.area.product_identity.value),),
        warnings=(),
        exclusions=(),
        configuration_key=context.configuration_key,
        live_state_fingerprint=fingerprint,
        extraction_revision=context.extraction_revision,
        workspace_revision=WorkspaceRevision(WORKSPACE_ID, 0),
        expected_workflow_version=WorkflowVersion(WORKFLOW_ID, workflow_version),
        engineering_policy=VersionedName("fake-engineering-policy", "1.0.0"),
        expires_at=now + timedelta(minutes=5),
        content_digest=digest,
        required_validation_steps=("run deterministic load flow",),
    )


def _authorization(
    gateway: DeterministicFakeGateway,
    proposal: ChangePreview | object,
    *,
    execution_id: str,
    operation_type: OperationType | None = None,
    digest: ContentDigest | None = None,
    register: bool = True,
    strategy: MutationStrategy = MutationStrategy.DIRECT_LEDGER,
) -> ExecutionAuthorization:
    context = gateway.active_context()
    authorization = ExecutionAuthorization(
        execution_id=execution_id,
        workflow_id=proposal.workflow_id,
        approval_request_id=f"approval:{execution_id}",
        authenticated_principal="fixture-engineer",
        proposal_digest=digest or proposal.content_digest,
        configuration_key=proposal.configuration_key,
        live_state_fingerprint=proposal.live_state_fingerprint,
        operation_type=operation_type or proposal.operation.operation_type,
        mutation_strategy=strategy,
        expected_workflow_version=proposal.expected_workflow_version,
        issued_at=context.extracted_at,
        expires_at=context.extracted_at + timedelta(minutes=2),
        agent_identity="fixture-agent",
        client_identity="fixture-client",
    )
    if register:
        gateway.register_test_authorization(authorization)
    return authorization


class DeterministicFakeGatewayTests(unittest.TestCase):
    def test_compatibility_name_and_complete_headline_flow(self) -> None:
        gateway = DeterministicFakeGateway()
        self.assertIsInstance(gateway, DeterministicHeadlineHarness)
        context = gateway.active_context()
        baseline = gateway.run_load_flow(
            context.configuration_key,
            (CommandSetting("network_representation", "balanced"),),
        )
        self.assertEqual(gateway.collect_violations(baseline.run_id, limit=10), ())

        preview = _scaled_preview(gateway, scale="2")
        authorization = _authorization(gateway, preview, execution_id="execute-change-1")
        applied = gateway.apply_change(preview, authorization)
        candidate = gateway.run_load_flow(
            context.configuration_key,
            (CommandSetting("network_representation", "balanced"),),
        )
        violations = gateway.collect_violations(candidate.run_id, limit=10)
        comparison = gateway.compare_results(baseline.run_id, candidate.run_id, limit=10)

        self.assertEqual(applied.verification_status.value, "verified")
        self.assertEqual(len(violations), 3)
        self.assertEqual(comparison.added_violation_ids, tuple(item.violation_id for item in violations))
        self.assertTrue(comparison.material_changes)

        plan = gateway.plan_rollback(
            applied.applied_change_id,
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 2),
        )
        rollback_authorization = _authorization(
            gateway,
            plan,
            execution_id="execute-rollback-1",
            operation_type=OperationType.ROLLBACK,
        )
        rollback = gateway.execute_rollback(plan, rollback_authorization)
        self.assertIsNone(rollback.baseline_reproduced)
        restored = gateway.run_load_flow(
            context.configuration_key,
            (CommandSetting("network_representation", "balanced"),),
        )
        verified_rollback = gateway.verify_rollback_baseline(
            rollback.rollback_result_id,
            baseline.run_id,
            restored.run_id,
            limit=10,
        )
        self.assertTrue(verified_rollback.baseline_reproduced)
        self.assertEqual(
            canonical_json(gateway.result_snapshot(baseline.run_id, limit=10)),
            canonical_json(gateway.result_snapshot(restored.run_id, limit=10)),
        )

    def test_fresh_instances_produce_identical_public_outputs(self) -> None:
        first = DeterministicFakeGateway()
        second = DeterministicFakeGateway()
        first_context = first.active_context()
        second_context = second.active_context()
        self.assertEqual(canonical_json(first_context), canonical_json(second_context))
        first_run = first.run_load_flow(first_context.configuration_key, ())
        second_run = second.run_load_flow(second_context.configuration_key, ())
        self.assertEqual(canonical_json(first_run), canonical_json(second_run))
        self.assertEqual(
            canonical_json(first.result_snapshot(first_run.run_id, limit=10)),
            canonical_json(second.result_snapshot(second_run.run_id, limit=10)),
        )

    def test_authorization_is_required_exactly_bound_and_single_use(self) -> None:
        gateway = DeterministicFakeGateway()
        preview = _scaled_preview(gateway, scale="1.1")
        with self.assertRaises(AuthorizationRequired):
            gateway.apply_change(preview, None)  # type: ignore[arg-type]
        invalid = _authorization(
            gateway,
            preview,
            execution_id="invalid-digest",
            digest=ContentDigest("content:v1:sha256:" + "f" * 64),
            register=False,
        )
        with self.assertRaises(AuthorizationInvalid):
            gateway.apply_change(preview, invalid)
        valid = _authorization(gateway, preview, execution_id="single-use")
        gateway.apply_change(preview, valid)
        with self.assertRaises(AuthorizationInvalid):
            gateway.apply_change(preview, valid)
        scenario = _authorization(
            DeterministicFakeGateway(),
            _scaled_preview(DeterministicFakeGateway(), scale="1.1"),
            execution_id="scenario",
            strategy=MutationStrategy.SCENARIO_ISOLATION,
            register=False,
        )
        with self.assertRaises(AuthorizationInvalid):
            gateway.register_test_authorization(scenario)

    def test_configuration_and_live_fingerprint_fail_closed(self) -> None:
        gateway = DeterministicFakeGateway()
        with self.assertRaises(ConfigurationMismatch):
            gateway.inventory(
                InventoryQuery(
                    ConfigurationKey(canonical_digest("another", kind="configuration-key")),
                    gateway.active_context().extraction_revision,
                    None,
                    None,
                    1,
                    None,
                )
            )
        preview = _scaled_preview(gateway, scale="1.1")
        gateway.force_external_change(gateway.load_1, "active_power", Quantity("12", "MW"))
        authorization = _authorization(gateway, preview, execution_id="stale-preview")
        with self.assertRaises(StaleContext):
            gateway.apply_change(preview, authorization)

    def test_inventory_is_bounded_deterministic_and_cursor_bound(self) -> None:
        gateway = DeterministicFakeGateway()
        configuration = gateway.active_context().configuration_key
        extraction_revision = gateway.active_context().extraction_revision
        query = InventoryQuery(configuration, extraction_revision, None, None, 2, None)
        result_1 = gateway.inventory(query)
        page_1, cursor = result_1.items, result_1.next_cursor
        self.assertEqual(len(page_1), 2)
        self.assertIsNotNone(cursor)
        self.assertNotIn(configuration.value, cursor.token)
        self.assertNotIn("query_digest", canonical_json(cursor))
        result_2 = gateway.inventory(InventoryQuery(configuration, extraction_revision, None, None, 2, cursor))
        page_2 = result_2.items
        self.assertEqual(
            tuple(item.product_identity.value for item in page_1 + page_2),
            tuple(sorted(item.product_identity.value for item in page_1 + page_2)),
        )
        with self.assertRaises(CursorInvalid):
            gateway.inventory(InventoryQuery(configuration, extraction_revision, AssetKind.LOAD, None, 2, cursor))
        with self.assertRaises(ValueError):
            gateway.inventory(InventoryQuery(configuration, extraction_revision, None, None, gateway.max_page_size + 1, None))

    def test_inventory_cursor_detects_tampering_and_expiry(self) -> None:
        now = [DeterministicFakeGateway().active_context().extracted_at]
        gateway = DeterministicFakeGateway(clock=lambda: now[0], cursor_secret=b"s" * 32)
        context = gateway.active_context()
        query = InventoryQuery(
            context.configuration_key,
            context.extraction_revision,
            None,
            None,
            2,
            None,
        )
        cursor = gateway.inventory(query).next_cursor
        self.assertIsNotNone(cursor)
        replacement = "A" if cursor.token[-1] != "A" else "B"
        tampered = PageCursor(cursor.token[:-1] + replacement)
        with self.assertRaises(CursorInvalid):
            gateway.inventory(InventoryQuery(
                context.configuration_key,
                context.extraction_revision,
                None,
                None,
                2,
                tampered,
            ))
        now[0] += timedelta(minutes=6)
        with self.assertRaises(CursorStale):
            gateway.inventory(InventoryQuery(
                context.configuration_key,
                context.extraction_revision,
                None,
                None,
                2,
                cursor,
            ))

    def test_non_convergence_is_a_structured_failure(self) -> None:
        gateway = DeterministicFakeGateway()
        preview = _scaled_preview(gateway, scale="4")
        gateway.apply_change(preview, _authorization(gateway, preview, execution_id="large-scale"))
        with self.assertRaises(CalculationNonConvergence) as caught:
            gateway.run_load_flow(gateway.active_context().configuration_key, ())
        self.assertEqual(caught.exception.category, GatewayErrorCategory.CALCULATION_NON_CONVERGENCE)

    def test_partial_mutation_is_recoverable_and_conflicts_fail_closed(self) -> None:
        gateway = DeterministicFakeGateway()
        preview = _scaled_preview(gateway, scale="1.2")
        gateway.inject_partial_mutation(after_writes=1)
        with self.assertRaises(PartialMutation) as caught:
            gateway.apply_change(preview, _authorization(gateway, preview, execution_id="partial"))
        partial = caught.exception.applied_change
        self.assertEqual(partial.verification_status.value, "reconciliation_required")
        plan = gateway.plan_rollback(
            partial.applied_change_id,
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 2),
        )
        rollback_authorization = _authorization(
            gateway,
            plan,
            execution_id="partial-rollback",
            operation_type=OperationType.ROLLBACK,
        )
        changed = plan.values_to_restore[0]
        gateway.force_external_change(
            changed.asset,
            changed.attribute,
            Quantity(Decimal(str(changed.current.value)) + Decimal("1"), changed.current.unit),
        )
        with self.assertRaises(RollbackConflictError):
            gateway.execute_rollback(plan, rollback_authorization)

    def test_workspace_workflow_cas_and_rollback_replay_fail_closed(self) -> None:
        gateway = DeterministicFakeGateway()
        preview = _scaled_preview(gateway, scale="1.1")
        applied = gateway.apply_change(
            preview,
            _authorization(gateway, preview, execution_id="cas-change"),
        )
        stale_preview = _scaled_preview(gateway, scale="1.1", preview_id="stale-preview")
        stale_authorization = _authorization(
            gateway,
            stale_preview,
            execution_id="stale-cas-change",
        )
        with self.assertRaises(StaleContext):
            gateway.apply_change(stale_preview, stale_authorization)

        plan = gateway.plan_rollback(
            applied.applied_change_id,
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 2),
        )
        gateway.execute_rollback(
            plan,
            _authorization(
                gateway,
                plan,
                execution_id="cas-rollback",
                operation_type=OperationType.ROLLBACK,
            ),
        )
        replay = _authorization(
            gateway,
            plan,
            execution_id="stale-plan-replay",
            operation_type=OperationType.ROLLBACK,
        )
        with self.assertRaises(StaleContext):
            gateway.execute_rollback(plan, replay)

    def test_concurrent_authorization_replay_applies_exactly_once(self) -> None:
        gateway = DeterministicFakeGateway()
        preview = _scaled_preview(gateway, scale="1.1")
        authorization = _authorization(gateway, preview, execution_id="concurrent-replay")

        def execute() -> str:
            try:
                gateway.apply_change(preview, authorization)
                return "applied"
            except AuthorizationInvalid:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = sorted(executor.map(lambda _: execute(), range(2)))
        self.assertEqual(outcomes, ["applied", "rejected"])
        reads = gateway.read_attributes(
            gateway.active_context().configuration_key,
            (gateway.load_1,),
            ("active_power",),
            limit=1,
        )
        self.assertEqual(reads[0][1][0].value, Quantity("11", "MW"))

    def test_command_settings_are_allowlisted_canonical_and_digest_complete(self) -> None:
        gateway = DeterministicFakeGateway()
        context = gateway.active_context()
        default_run = gateway.run_load_flow(context.configuration_key, ())
        explicit_run = gateway.run_load_flow(
            context.configuration_key,
            (
                CommandSetting("solver_tolerance", Quantity("0.0001", "p.u.")),
                CommandSetting("network_representation", "balanced"),
                CommandSetting("calculate_voltage_drops", True),
            ),
        )
        self.assertEqual(default_run.command_settings, explicit_run.command_settings)
        self.assertEqual(default_run.calculation_input_digest, explicit_run.calculation_input_digest)
        self.assertEqual(
            tuple(setting.name for setting in default_run.command_settings),
            tuple(sorted(setting.name for setting in default_run.command_settings)),
        )
        with self.assertRaises(InvalidOperation):
            gateway.run_load_flow(
                context.configuration_key,
                (CommandSetting("network_representation", "balanced"),) * 2,
            )
        with self.assertRaises(InvalidOperation):
            gateway.run_load_flow(
                context.configuration_key,
                (CommandSetting("unrestricted_vendor_option", True),),
            )

    def test_live_and_result_reads_require_explicit_bounds(self) -> None:
        gateway = DeterministicFakeGateway()
        context = gateway.active_context()
        with self.assertRaises(InvalidOperation):
            gateway.read_attributes(
                context.configuration_key,
                (gateway.load_1, gateway.load_2),
                ("active_power", "reactive_power"),
                limit=3,
            )
        run = gateway.run_load_flow(context.configuration_key, ())
        with self.assertRaises(InvalidOperation):
            gateway.result_snapshot(run.run_id, limit=2)

        preview = _scaled_preview(gateway, scale="2")
        gateway.apply_change(preview, _authorization(gateway, preview, execution_id="bounded-results"))
        candidate = gateway.run_load_flow(context.configuration_key, ())
        with self.assertRaises(InvalidOperation):
            gateway.collect_violations(candidate.run_id, limit=2)
        with self.assertRaises(InvalidOperation):
            gateway.compare_results(run.run_id, candidate.run_id, limit=2)

    def test_gateway_error_records_are_bounded_redacted_and_serializable(self) -> None:
        error = CalculationNonConvergence(
            "password=hunter2 failed at /Users/example/private/model.pfd",
            details={"total_active_power": "120 MW"},
        )
        record = error.to_record()
        serialized = canonical_json(record)
        self.assertIn("[redacted]", serialized)
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("/Users/example", serialized)
        self.assertLessEqual(len(error.message), 256)
        with self.assertRaises(ValueError):
            InvalidOperation("failed", details={"password": "secret"})
        with self.assertRaises(TypeError):
            InvalidOperation("failed", details={"safe_key": object()})  # type: ignore[dict-item]

    def test_error_categories_cover_the_buildout_contract(self) -> None:
        self.assertEqual(
            {item.value for item in GatewayErrorCategory},
            {
                "connection_failure",
                "configuration_mismatch",
                "object_not_found",
                "object_ambiguous",
                "stale_context",
                "invalid_operation",
                "authorization_failure",
                "authorization_required",
                "authorization_invalid",
                "calculation_non_convergence",
                "partial_mutation",
                "rollback_conflict",
                "lease_lost",
                "operation_still_in_flight",
                "reconciliation_required",
                "cursor_invalid",
                "cursor_stale",
            },
        )


if __name__ == "__main__":
    unittest.main()
