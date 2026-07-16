from __future__ import annotations

import platform
import sys
import unittest
from datetime import datetime, timezone
from types import ModuleType

from powerfactory_agent.domain import (
    AttributeKind,
    AttributeSelector,
    CommandExecutionRequest,
    CommandKind,
    CommandSelector,
    ConfigurationKey,
    ContextActivationRequest,
    DependencyReadRequest,
    ObjectClassKind,
    ObjectClassSelector,
    ObjectQueryRequest,
    ObjectQueryScope,
    OutOfServicePolicy,
    RelationshipKind,
    RelationshipSelector,
    ResultCollectionRequest,
    ResultVariableKind,
    ResultVariableSelector,
    SessionStartRequest,
    VersionedName,
)
from powerfactory_agent.gateway import (
    NativeMappingUnavailable,
    NativePowerFactory2026Config,
    NativePowerFactory2026Vendor,
    PowerFactoryGateway2026,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
CONTRACT = VersionedName("native-test", "v1")
KEY = ConfigurationKey(f"configuration-key:v1:sha256:{'a' * 64}")


class NativeObject:
    def __init__(
        self, name: str, class_name: str, path: str, attributes=None, units=None, parent=None
    ):
        self.loc_name = name
        self._class_name = class_name
        self._path = path
        self._attributes = dict(attributes or {})
        self._units = dict(units or {})
        self._parent = parent
        self.activation_count = 0

    def GetFullName(self):
        return self._path

    def GetClassName(self):
        return self._class_name

    def GetAttribute(self, name):
        if name == "loc_name":
            return self.loc_name
        if name not in self._attributes:
            raise AttributeError(name)
        return self._attributes[name]

    def GetAttributeUnit(self, name):
        return self._units.get(name)

    def GetParent(self):
        return self._parent

    def Activate(self):
        self.activation_count += 1
        return 0


class NativeFolder:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def GetContents(self, pattern, recursive):
        self.calls.append((pattern, recursive))
        suffix = pattern.removeprefix("*.")
        return [item for item in self.values if item.GetClassName() == suffix]


class NativeCommand:
    def Execute(self):
        return 0


class NativeApplication:
    def __init__(self):
        self.project = NativeObject("Project", "IntPrj", "Project.IntPrj")
        self.study = NativeObject("Study", "IntCase", "Project/Study.IntCase")
        self.grid = NativeObject("Grid", "ElmNet", "Project/Grid.ElmNet")
        self.terminal = NativeObject(
            "Bus A",
            "ElmTerm",
            "Project/Grid/Bus A.ElmTerm",
            {"outserv": 0, "uknom": 110, "m:u": 1.01},
            {"uknom": "kV", "m:u": "p.u."},
        )
        cubic = NativeObject(
            "Cubicle", "StaCubic", "Project/Grid/Cubicle.StaCubic", {"cterm": self.terminal}
        )
        self.load = NativeObject(
            "Load A",
            "ElmLod",
            "Project/Grid/Load A.ElmLod",
            {"outserv": 0, "plini": 10, "qlini": 2, "bus1": cubic},
            {"plini": "MW", "qlini": "Mvar"},
        )
        self.command = NativeCommand()
        self.current_user = NativeFolder([self.project])
        self.folders = {
            "study": NativeFolder([self.study]),
            "scen": NativeFolder([]),
        }

    def GetVersion(self):
        return "2026 SP0"

    def GetActiveProject(self):
        return self.project

    def GetActiveStudyCase(self):
        return self.study

    def GetActiveScenario(self):
        return None

    def GetActiveStages(self):
        return []

    def GetCalcRelevantObjects(self, pattern, include_out_of_service):
        suffix = pattern.removeprefix("*.")
        return [
            item for item in (self.grid, self.terminal, self.load) if item.GetClassName() == suffix
        ]

    def GetCurrentUser(self):
        return self.current_user

    def GetProjectFolder(self, name):
        return self.folders[name]

    def ActivateProject(self, key):
        return 0

    def GetFromStudyCase(self, class_name):
        return self.command if class_name == "ComLdf" else None


class NativePowerFactory2026VendorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = NativeApplication()
        module = ModuleType("powerfactory")
        module.GetApplicationExt = lambda profile, password, ini: self.application
        accepted = frozenset(
            {
                "context.active",
                "context.activate",
                "class.grid",
                "class.terminal",
                "class.load",
                "attribute.display_name",
                "attribute.nominal_voltage",
                "attribute.active_power",
                "attribute.reactive_power",
                "relationship.connected_terminal",
                "command.load_flow",
                "result.bus_voltage",
            }
        )
        config = NativePowerFactory2026Config(
            "powerfactory.pyd",
            "install-a",
            "profile-a",
            sys.implementation.cache_tag,
            platform.machine(),
            accepted,
        )
        ids = iter(("session-1", "execution-1"))
        self.vendor = NativePowerFactory2026Vendor(
            config,
            module_loader=lambda path: module,
            id_factory=lambda: next(ids),
        )
        self.gateway = PowerFactoryGateway2026(self.vendor, clock=lambda: NOW)
        self.gateway.start(SessionStartRequest("install-a", "profile-a", "2026", "SP0", True))
        self.context = self.gateway.inspect_context()
        self.load_class = ObjectClassSelector(ObjectClassKind.LOAD, CONTRACT)
        self.terminal_class = ObjectClassSelector(ObjectClassKind.TERMINAL, CONTRACT)

    def test_context_activation_discovers_projects_from_current_user(self) -> None:
        observation = self.gateway.activate_context(
            ContextActivationRequest("Project.IntPrj", "Project/Study.IntCase", None)
        )

        self.assertTrue(observation.context.verified)
        self.assertEqual(self.application.current_user.calls, [("*.IntPrj", 1)])
        self.assertEqual(self.application.study.activation_count, 0)

    def test_active_context_inventory_and_dependencies_are_real_boundary_reads(self) -> None:
        self.assertTrue(self.context.verified)
        power = AttributeSelector(AttributeKind.ACTIVE_POWER, CONTRACT)
        query = self.gateway.query_objects(
            ObjectQueryRequest(
                self.context.configuration_key,
                ObjectQueryScope.ACTIVE_GRIDS,
                OutOfServicePolicy.EXCLUDE,
                (self.load_class,),
                (power,),
                10,
                None,
            )
        )
        self.assertEqual("10", str(query.records[0].fields[0].value.value))
        relationship = RelationshipSelector(RelationshipKind.CONNECTED_TERMINAL, CONTRACT)
        dependencies = self.gateway.observe_dependencies(
            DependencyReadRequest(
                self.context.configuration_key,
                (query.records[0].selector,),
                (power,),
                (relationship,),
                10,
            )
        )
        self.assertTrue(dependencies.complete)
        self.assertEqual(
            ObjectClassKind.TERMINAL,
            dependencies.objects[0].relationships[0].target.object_class.kind,
        )

    def test_grid_inventory_does_not_invent_an_outserv_attribute(self) -> None:
        grid = ObjectClassSelector(ObjectClassKind.GRID, CONTRACT)
        query = self.gateway.query_objects(
            ObjectQueryRequest(
                self.context.configuration_key,
                ObjectQueryScope.ACTIVE_GRIDS,
                OutOfServicePolicy.EXCLUDE,
                (grid,),
                (),
                10,
                None,
            )
        )
        self.assertEqual("Grid", query.records[0].display_name)

    def test_comldf_results_and_cleanup_do_not_expose_a_fake_fallback(self) -> None:
        command = CommandSelector(CommandKind.LOAD_FLOW, CONTRACT)
        execution = self.gateway.execute_command(
            CommandExecutionRequest(self.context.configuration_key, command, (), "load-flow-1")
        )
        terminal = self.vendor._selector(self.application.terminal, self.terminal_class)
        voltage = ResultVariableSelector(ResultVariableKind.BUS_VOLTAGE, CONTRACT)
        results = self.gateway.collect_results(
            ResultCollectionRequest(
                self.context.configuration_key,
                execution.execution_id,
                (terminal,),
                (voltage,),
                10,
                None,
            )
        )
        self.assertEqual("1.01", results.rows[0].cells[0].source_value)
        self.assertTrue(self.gateway.close().cleanup_succeeded)

    def test_unaccepted_mapping_is_a_typed_fail_closed_error(self) -> None:
        unsupported = AttributeSelector(AttributeKind.REACTIVE_POWER, CONTRACT)
        self.vendor._config = NativePowerFactory2026Config(
            "powerfactory.pyd",
            "install-a",
            "profile-a",
            sys.implementation.cache_tag,
            platform.machine(),
            self.vendor._config.accepted_mappings - {"attribute.reactive_power"},
        )
        with self.assertRaises(NativeMappingUnavailable):
            self.gateway.query_objects(
                ObjectQueryRequest(
                    self.context.configuration_key,
                    ObjectQueryScope.ACTIVE_GRIDS,
                    OutOfServicePolicy.EXCLUDE,
                    (self.load_class,),
                    (unsupported,),
                    10,
                    None,
                )
            )


if __name__ == "__main__":
    unittest.main()
