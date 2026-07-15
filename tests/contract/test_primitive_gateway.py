from __future__ import annotations

from decimal import Decimal
import unittest

from powerfactory_agent.domain import (
    AttributeSelector,
    AttributeWriteDisposition,
    AttributeWriteRequest,
    CommandExecutionRequest,
    CommandSetting,
    ContextActivationRequest,
    DependencyReadRequest,
    LogReadRequest,
    ObjectClassSelector,
    ObjectQueryRequest,
    ObjectQueryScope,
    OutOfServicePolicy,
    PageCursor,
    Quantity,
    ResultCellStatus,
    ResultCollectionRequest,
    SessionStartRequest,
    VersionedName,
)
from powerfactory_agent.gateway import (
    ConfigurationMismatch,
    CursorInvalid,
    DeterministicPrimitiveGateway,
    InvalidOperation,
    PowerFactoryGateway,
)
from powerfactory_agent.serialization import canonical_json, from_json, to_primitive


def _start_request(*, read_only: bool = True) -> SessionStartRequest:
    return SessionStartRequest("pf-2026-fixture", "profile-fixture", "2026", "SP0", read_only)


def _activate(gateway: DeterministicPrimitiveGateway, *, read_only: bool = True):
    gateway.start(_start_request(read_only=read_only))
    return gateway.activate_context(ContextActivationRequest("project-fixture", "study-fixture", None)).context


class PrimitiveGatewayContractTests(unittest.TestCase):
    def test_selector_wrappers_reject_raw_strings(self) -> None:
        with self.assertRaises(TypeError):
            ObjectClassSelector("load", VersionedName("gateway-object-class", "v1"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            AttributeSelector("active_power", VersionedName("gateway-attribute", "v1"))  # type: ignore[arg-type]

    def test_constructor_is_side_effect_free_and_runtime_protocol_is_narrow(self) -> None:
        gateway = DeterministicPrimitiveGateway()
        self.assertIsInstance(gateway, PowerFactoryGateway)
        self.assertFalse(gateway.started)
        with self.assertRaises(InvalidOperation):
            gateway.inspect_context()

    def test_start_is_explicit_idempotent_and_rejects_a_different_request(self) -> None:
        gateway = DeterministicPrimitiveGateway()
        request = _start_request()
        first = gateway.start(request)
        self.assertEqual(first, gateway.start(request))
        with self.assertRaises(InvalidOperation):
            gateway.start(_start_request(read_only=False))

    def test_activation_observes_variant_stages_and_binds_configuration(self) -> None:
        gateway = DeterministicPrimitiveGateway()
        gateway.start(_start_request())
        self.assertFalse(gateway.inspect_context().verified)
        observation = gateway.activate_context(ContextActivationRequest("project-fixture", "study-fixture", None))
        self.assertTrue(observation.context.verified)
        self.assertTrue(observation.context.variant_stages[0].active)
        self.assertEqual(("grid-fixture",), observation.context.active_grid_keys)

        other = DeterministicPrimitiveGateway()
        other.start(_start_request())
        other_context = other.activate_context(
            ContextActivationRequest("project-fixture", "study-fixture", "scenario-1")
        ).context
        with self.assertRaises(ConfigurationMismatch):
            gateway.query_objects(
                ObjectQueryRequest(
                    other_context.configuration_key,
                    ObjectQueryScope.ACTIVE_GRIDS,
                    OutOfServicePolicy.EXCLUDE,
                    (gateway.load_class,),
                    (),
                    1,
                    None,
                )
            )

    def test_object_query_exposes_scope_policy_completeness_warnings_and_bound_cursor(self) -> None:
        gateway = DeterministicPrimitiveGateway()
        context = _activate(gateway)
        first_request = ObjectQueryRequest(
            context.configuration_key,
            ObjectQueryScope.ACTIVE_GRIDS,
            OutOfServicePolicy.EXCLUDE,
            (gateway.terminal_class, gateway.load_class),
            (gateway.display_name_attribute,),
            1,
            None,
        )
        first = gateway.query_objects(first_request)
        self.assertFalse(first.complete)
        self.assertTrue(first.truncated)
        self.assertEqual("query_truncated", first.warnings[0].code.value)
        second = gateway.query_objects(
            ObjectQueryRequest(
                first_request.configuration_key,
                first_request.scope,
                first_request.out_of_service,
                first_request.object_classes,
                first_request.attributes,
                1,
                first.next_cursor,
            )
        )
        self.assertTrue(second.complete)
        self.assertFalse(second.truncated)
        with self.assertRaises(CursorInvalid):
            gateway.query_objects(
                ObjectQueryRequest(
                    context.configuration_key,
                    ObjectQueryScope.ACTIVE_PROJECT,
                    first_request.out_of_service,
                    first_request.object_classes,
                    first_request.attributes,
                    1,
                    first.next_cursor,
                )
            )
        with self.assertRaises(CursorInvalid):
            gateway.query_objects(
                ObjectQueryRequest(
                    context.configuration_key,
                    first_request.scope,
                    first_request.out_of_service,
                    first_request.object_classes,
                    first_request.attributes,
                    1,
                    PageCursor("bad.payload"),
                )
            )

    def test_dependency_fingerprint_covers_full_deterministic_observation(self) -> None:
        gateway = DeterministicPrimitiveGateway()
        context = _activate(gateway)
        request = DependencyReadRequest(
            context.configuration_key,
            (gateway.load,),
            (gateway.active_power_attribute, gateway.reactive_power_attribute),
            (gateway.connected_terminal_relationship,),
            2,
        )
        first = gateway.observe_dependencies(request)
        second = gateway.observe_dependencies(request)
        self.assertTrue(first.complete)
        self.assertEqual(first.fingerprint, second.fingerprint)
        narrower = gateway.observe_dependencies(
            DependencyReadRequest(
                context.configuration_key,
                (gateway.load,),
                (gateway.active_power_attribute,),
                (),
                1,
            )
        )
        self.assertNotEqual(first.fingerprint, narrower.fingerprint)

    def test_command_and_results_are_separate_and_every_cell_has_status_and_evidence(self) -> None:
        gateway = DeterministicPrimitiveGateway()
        context = _activate(gateway)
        request = CommandExecutionRequest(
            context.configuration_key,
            gateway.load_flow_command,
            (CommandSetting("iopt_net", "balanced"),),
            "load-flow-1",
        )
        execution = gateway.execute_command(request)
        self.assertEqual(execution, gateway.execute_command(request))
        results = gateway.collect_results(
            ResultCollectionRequest(
                context.configuration_key,
                execution.execution_id,
                (gateway.bus,),
                (
                    gateway.bus_voltage_result,
                    gateway.active_power_result,
                    gateway.rotor_angle_result,
                    gateway.bus_current_result,
                ),
                1,
                None,
            )
        )
        by_status = {cell.status: cell for cell in results.rows[0].cells}
        self.assertEqual(
            {
                ResultCellStatus.AVAILABLE,
                ResultCellStatus.MISSING,
                ResultCellStatus.UNSUPPORTED,
                ResultCellStatus.NON_FINITE,
            },
            set(by_status),
        )
        available = by_status[ResultCellStatus.AVAILABLE]
        self.assertEqual("1.010000", available.source_value)
        self.assertEqual(Quantity(Decimal("1.01"), "p.u."), available.normalized)
        self.assertEqual("nan", by_status[ResultCellStatus.NON_FINITE].source_value)

    def test_logs_honor_entry_and_byte_bounds_and_report_truncation_and_redaction(self) -> None:
        gateway = DeterministicPrimitiveGateway()
        context = _activate(gateway)
        execution = gateway.execute_command(
            CommandExecutionRequest(context.configuration_key, gateway.load_flow_command, (), "load-flow-1")
        )
        bounded = gateway.read_logs(LogReadRequest(None, 1, 65_536, None))
        self.assertEqual(1, len(bounded.entries))
        self.assertTrue(bounded.truncated)
        self.assertLessEqual(bounded.bytes_returned, 65_536)
        self.assertFalse(bounded.redaction_applied)
        command_logs = gateway.read_logs(LogReadRequest(execution.execution_id, 10, 65_536, None))
        self.assertTrue(command_logs.entries)
        self.assertTrue(all(item.execution_id == execution.execution_id for item in command_logs.entries))
        byte_limited = gateway.read_logs(LogReadRequest(None, 10, 1, None))
        self.assertEqual((), byte_limited.entries)
        self.assertTrue(byte_limited.truncated)
        self.assertEqual(0, byte_limited.bytes_returned)

    def test_writes_are_closed_by_default_and_cover_all_effect_dispositions(self) -> None:
        read_only = DeterministicPrimitiveGateway()
        context = _activate(read_only)
        request = AttributeWriteRequest(
            context.configuration_key,
            read_only.load,
            read_only.active_power_attribute,
            Quantity(Decimal("10"), "MW"),
            Quantity(Decimal("11"), "MW"),
            "test-write-1",
        )
        with self.assertRaises(InvalidOperation):
            read_only.write_attribute(request)

        writable = DeterministicPrimitiveGateway(allow_test_writes=True)
        writable_context = _activate(writable, read_only=False)
        confirmed = writable.write_attribute(
            AttributeWriteRequest(
                writable_context.configuration_key,
                writable.load,
                writable.active_power_attribute,
                Quantity(Decimal("10"), "MW"),
                Quantity(Decimal("11"), "MW"),
                "test-write-1",
            )
        )
        self.assertIs(confirmed.disposition, AttributeWriteDisposition.CONFIRMED)
        rejected = writable.write_attribute(
            AttributeWriteRequest(
                writable_context.configuration_key,
                writable.load,
                writable.active_power_attribute,
                Quantity(Decimal("10"), "MW"),
                Quantity(Decimal("12"), "MW"),
                "test-write-2",
            )
        )
        self.assertIs(rejected.disposition, AttributeWriteDisposition.PRECONDITION_REJECTED)

        uncertain_gateway = DeterministicPrimitiveGateway(
            allow_test_writes=True,
            inject_effect_uncertain=True,
        )
        uncertain_context = _activate(uncertain_gateway, read_only=False)
        uncertain = uncertain_gateway.write_attribute(
            AttributeWriteRequest(
                uncertain_context.configuration_key,
                uncertain_gateway.load,
                uncertain_gateway.active_power_attribute,
                Quantity(Decimal("10"), "MW"),
                Quantity(Decimal("11"), "MW"),
                "test-write-uncertain",
            )
        )
        self.assertIs(uncertain.disposition, AttributeWriteDisposition.EFFECT_UNCERTAIN)
        self.assertIsNone(uncertain.confirmed)

    def test_boundary_requests_and_observations_round_trip_strictly(self) -> None:
        gateway = DeterministicPrimitiveGateway()
        start_request = _start_request()
        session = gateway.start(start_request)
        activation_request = ContextActivationRequest("project-fixture", "study-fixture", None)
        activation = gateway.activate_context(activation_request)
        query_request = ObjectQueryRequest(
            activation.context.configuration_key,
            ObjectQueryScope.ACTIVE_GRIDS,
            OutOfServicePolicy.INCLUDE,
            (gateway.load_class,),
            (gateway.active_power_attribute,),
            1,
            None,
        )
        batch = gateway.query_objects(query_request)
        for model in (start_request, session, activation_request, activation, query_request, batch):
            with self.subTest(model=type(model).__name__):
                payload = canonical_json(model)
                self.assertEqual(model, from_json(type(model), payload))
                self.assertIsInstance(to_primitive(model), dict)

    def test_close_is_idempotent(self) -> None:
        gateway = DeterministicPrimitiveGateway()
        gateway.start(_start_request())
        first = gateway.close()
        second = gateway.close()
        self.assertTrue(first.was_open)
        self.assertFalse(second.was_open)
        self.assertTrue(first.cleanup_succeeded)
        self.assertTrue(second.cleanup_succeeded)


if __name__ == "__main__":
    unittest.main()
