from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from powerfactory_agent.domain import (
    AttributeKind,
    AttributeSelector,
    AttributeWriteRequest,
    CommandExecutionRequest,
    CommandKind,
    CommandSelector,
    ContextActivationRequest,
    DependencyReadRequest,
    LogReadRequest,
    LogSeverity,
    ObjectClassKind,
    ObjectClassSelector,
    ObjectQueryRequest,
    ObjectQueryScope,
    OutOfServicePolicy,
    PrimitiveField,
    PrimitiveObjectSelector,
    Quantity,
    ResultCell,
    ResultCellStatus,
    ResultCollectionRequest,
    ResultVariableKind,
    ResultVariableSelector,
    SessionStartRequest,
    VariantStageObservation,
    VersionedName,
)
from powerfactory_agent.gateway import (
    ConfigurationMismatch,
    InvalidOperation,
    PowerFactory2026Vendor,
    PowerFactoryGateway,
    PowerFactoryGateway2026,
)
from powerfactory_agent.gateway.powerfactory2026 import (
    VendorCommandOutcome,
    VendorContext,
    VendorDependencyRecord,
    VendorLogRecord,
    VendorObjectRecord,
    VendorResultRecord,
    VendorSession,
)
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
OBJECT_CONTRACT = VersionedName("gateway-object-class", "v1")
ATTRIBUTE_CONTRACT = VersionedName("gateway-attribute", "v1")
COMMAND_CONTRACT = VersionedName("gateway-command", "v1")
RESULT_CONTRACT = VersionedName("gateway-result-variable", "v1")


class FakePowerFactory2026Vendor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.context = VendorContext(None, None, None, (), ())
        self.mismatch_activation = False
        self.command_return_code = 0
        self.object_class = ObjectClassSelector(ObjectClassKind.LOAD, OBJECT_CONTRACT)
        self.name_attribute = AttributeSelector(AttributeKind.DISPLAY_NAME, ATTRIBUTE_CONTRACT)
        self.power_attribute = AttributeSelector(AttributeKind.ACTIVE_POWER, ATTRIBUTE_CONTRACT)
        self.command = CommandSelector(CommandKind.LOAD_FLOW, COMMAND_CONTRACT)
        self.voltage = ResultVariableSelector(ResultVariableKind.BUS_VOLTAGE, RESULT_CONTRACT)
        self.load_a = PrimitiveObjectSelector(
            "project-a", self.object_class, "for_name", "Load A", "Grid/Load A.ElmLod"
        )
        self.load_b = PrimitiveObjectSelector(
            "project-a", self.object_class, "for_name", "Load B", "Grid/Load B.ElmLod"
        )

    def start(self, request: SessionStartRequest) -> VendorSession:
        self.calls.append("start")
        return VendorSession(
            "vendor-session-1",
            "2026 SP0",
            "cp313",
            "x86_64",
            ("activate_context", "query_objects", "observe_dependencies", "execute_command", "collect_results", "read_logs"),
        )

    def inspect_context(self) -> VendorContext:
        self.calls.append("inspect_context")
        return self.context

    def activate_context(self, request: ContextActivationRequest) -> VendorContext:
        self.calls.append("activate_context")
        if self.mismatch_activation:
            return VendorContext("other-project", request.study_case_key, request.operational_scenario_key, (), ("grid-a",))
        self.context = VendorContext(
            request.project_key,
            request.study_case_key,
            request.operational_scenario_key,
            (VariantStageObservation("variant-a", "stage-a", True),),
            ("grid-a",),
        )
        return self.context

    def query_objects(self, request: ObjectQueryRequest):
        self.calls.append("query_objects")
        return (
            VendorObjectRecord(
                self.load_a,
                "Load A",
                (PrimitiveField(self.name_attribute, "Load A"), PrimitiveField(self.power_attribute, Quantity(Decimal("10"), "MW"))),
                False,
            ),
            VendorObjectRecord(
                self.load_b,
                "Load B",
                (PrimitiveField(self.name_attribute, "Load B"),),
                False,
            ),
        )

    def observe_dependencies(self, request: DependencyReadRequest):
        self.calls.append("observe_dependencies")
        return (
            (
                VendorDependencyRecord(
                    self.load_a,
                    (PrimitiveField(self.power_attribute, Quantity(Decimal("10"), "MW")),),
                    (),
                ),
            ),
            True,
        )

    def execute_command(self, request: CommandExecutionRequest) -> VendorCommandOutcome:
        self.calls.append("execute_command")
        return VendorCommandOutcome(
            "execution-1",
            self.command_return_code,
            ("password=not-returned",) if self.command_return_code else (),
        )

    def collect_results(self, request: ResultCollectionRequest):
        self.calls.append("collect_results")
        return (
            VendorResultRecord(
                self.load_a,
                (
                    ResultCell(
                        self.voltage,
                        ResultCellStatus.AVAILABLE,
                        "1.010000",
                        "p.u.",
                        Quantity(Decimal("1.01"), "p.u."),
                        None,
                    ),
                ),
            ),
        )

    def read_logs(self):
        self.calls.append("read_logs")
        return (VendorLogRecord(None, LogSeverity.INFO, "vendor", "password=not-returned"),)

    def close(self):
        self.calls.append("close")
        return ("vendor cleanup complete",)


def _start_request() -> SessionStartRequest:
    return SessionStartRequest("installation-a", "profile-a", "2026", "SP0", True)


def _activate(gateway: PowerFactoryGateway2026):
    gateway.start(_start_request())
    return gateway.activate_context(ContextActivationRequest("project-a", "study-a", None)).context


class PowerFactoryGateway2026ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vendor = FakePowerFactory2026Vendor()
        self.gateway = PowerFactoryGateway2026(
            self.vendor,
            clock=lambda: NOW,
        )

    def test_constructor_is_side_effect_free_and_start_is_idempotent(self) -> None:
        self.assertIsInstance(self.vendor, PowerFactory2026Vendor)
        self.assertIsInstance(self.gateway, PowerFactoryGateway)
        self.assertFalse(self.gateway.started)
        self.assertEqual([], self.vendor.calls)
        first = self.gateway.start(_start_request())
        self.assertEqual(first, self.gateway.start(_start_request()))
        self.assertEqual(["start"], self.vendor.calls)

    def test_context_activation_is_post_verified(self) -> None:
        context = _activate(self.gateway)
        self.assertTrue(context.verified)
        self.assertEqual("project-a", context.project_key)
        self.vendor.mismatch_activation = True
        with self.assertRaises(ConfigurationMismatch):
            self.gateway.activate_context(ContextActivationRequest("project-a", "study-a", None))

    def test_query_is_bounded_and_cursor_scoped(self) -> None:
        context = _activate(self.gateway)
        request = ObjectQueryRequest(
            context.configuration_key,
            ObjectQueryScope.ACTIVE_GRIDS,
            OutOfServicePolicy.EXCLUDE,
            (self.vendor.object_class,),
            (self.vendor.name_attribute,),
            1,
            None,
        )
        first = self.gateway.query_objects(request)
        self.assertEqual(1, len(first.records))
        self.assertTrue(first.truncated)
        second = self.gateway.query_objects(
            ObjectQueryRequest(
                request.configuration_key,
                request.scope,
                request.out_of_service,
                request.object_classes,
                request.attributes,
                1,
                first.next_cursor,
            )
        )
        self.assertTrue(second.complete)
        self.assertEqual("Load B", second.records[0].display_name)

    def test_dependency_observation_is_fingerprinted(self) -> None:
        context = _activate(self.gateway)
        request = DependencyReadRequest(
            context.configuration_key,
            (self.vendor.load_a,),
            (self.vendor.power_attribute,),
            (),
            1,
        )
        first = self.gateway.observe_dependencies(request)
        second = self.gateway.observe_dependencies(request)
        self.assertTrue(first.complete)
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_command_execution_and_result_collection_are_separate(self) -> None:
        context = _activate(self.gateway)
        execution = self.gateway.execute_command(
            CommandExecutionRequest(context.configuration_key, self.vendor.command, (), "command-a")
        )
        self.assertEqual(0, execution.return_code)
        self.assertNotIn("collect_results", self.vendor.calls)
        results = self.gateway.collect_results(
            ResultCollectionRequest(
                context.configuration_key,
                execution.execution_id,
                (self.vendor.load_a,),
                (self.vendor.voltage,),
                1,
                None,
            )
        )
        self.assertEqual("1.010000", results.rows[0].cells[0].source_value)
        self.vendor.command_return_code = 3
        failed = self.gateway.execute_command(
            CommandExecutionRequest(context.configuration_key, self.vendor.command, (), "command-b")
        )
        with self.assertRaises(InvalidOperation):
            self.gateway.collect_results(
                ResultCollectionRequest(
                    context.configuration_key,
                    failed.execution_id,
                    (self.vendor.load_a,),
                    (self.vendor.voltage,),
                    1,
                    None,
                )
            )

    def test_logs_are_bounded_and_redacted(self) -> None:
        _activate(self.gateway)
        logs = self.gateway.read_logs(LogReadRequest(None, 10, 65_536, None))
        self.assertTrue(logs.entries)
        self.assertTrue(logs.redaction_applied)
        self.assertTrue(all("not-returned" not in item.message for item in logs.entries))
        self.assertTrue(any(item.category == "vendor" for item in logs.entries))
        limited = self.gateway.read_logs(LogReadRequest(None, 10, 1, None))
        self.assertEqual((), limited.entries)
        self.assertTrue(limited.truncated)

    def test_writes_are_disabled_and_close_is_idempotent(self) -> None:
        context = _activate(self.gateway)
        with self.assertRaises(InvalidOperation):
            self.gateway.write_attribute(
                AttributeWriteRequest(
                    context.configuration_key,
                    self.vendor.load_a,
                    self.vendor.power_attribute,
                    Quantity(Decimal("10"), "MW"),
                    Quantity(Decimal("11"), "MW"),
                    "write-a",
                )
            )
        first = self.gateway.close()
        second = self.gateway.close()
        self.assertTrue(first.was_open)
        self.assertFalse(second.was_open)
        self.assertTrue(first.cleanup_succeeded)
        self.assertEqual(1, self.vendor.calls.count("close"))


if __name__ == "__main__":
    unittest.main()
