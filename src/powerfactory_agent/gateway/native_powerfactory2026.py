"""Windows-only PowerFactory 2026 vendor implementation.

The module is importable without PowerFactory.  The vendor extension is loaded
only by :meth:`NativePowerFactory2026Vendor.start`, and every native mapping is
disabled until its evidence key is explicitly admitted by configuration.
"""

from __future__ import annotations

import importlib.util
import math
import os
import platform
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation as DecimalInvalidOperation
from types import ModuleType
from typing import Any
from uuid import uuid4

from powerfactory_agent.domain import (
    AttributeKind,
    CommandExecutionRequest,
    CommandKind,
    DependencyReadRequest,
    LogSeverity,
    ObjectClassKind,
    ObjectQueryRequest,
    ObjectQueryScope,
    PrimitiveField,
    PrimitiveObjectSelector,
    Quantity,
    RelationshipKind,
    RelationshipObservation,
    ResultCell,
    ResultCellStatus,
    ResultCollectionRequest,
    ResultVariableKind,
    SessionStartRequest,
    VariantStageObservation,
)
from powerfactory_agent.gateway.errors import (
    ConfigurationMismatch,
    InvalidOperation,
    ObjectAmbiguous,
    ObjectNotFound,
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

ModuleLoader = Callable[[str], ModuleType]
IdFactory = Callable[[], str]

_CLASS_NAMES = {
    ObjectClassKind.GRID: "ElmNet",
    ObjectClassKind.TERMINAL: "ElmTerm",
    ObjectClassKind.LINE: "ElmLne",
    ObjectClassKind.LOAD: "ElmLod",
    ObjectClassKind.TRANSFORMER: "ElmTr2",
}
_ATTRIBUTES = {
    AttributeKind.DISPLAY_NAME: ("loc_name", None),
    AttributeKind.NOMINAL_VOLTAGE: ("uknom", "kV"),
    AttributeKind.ACTIVE_POWER: ("plini", "MW"),
    AttributeKind.REACTIVE_POWER: ("qlini", "Mvar"),
}
_RESULTS = {
    ResultVariableKind.BUS_VOLTAGE: ("m:u", "p.u."),
    ResultVariableKind.EQUIPMENT_LOADING: ("c:loading", "%"),
}
_CONNECTED_TERMINAL_PATHS = {
    ObjectClassKind.LOAD: (("bus1", "cterm"),),
    ObjectClassKind.LINE: (("bus1", "cterm"), ("bus2", "cterm")),
    ObjectClassKind.TRANSFORMER: (("bushv", "cterm"), ("buslv", "cterm")),
}


class NativeMappingUnavailable(InvalidOperation):
    """A candidate PowerFactory mapping lacks accepted evidence."""


@dataclass(frozen=True, slots=True)
class NativePowerFactory2026Config:
    pyd_path: str
    installation_id: str
    profile_id: str
    expected_python_abi: str
    expected_architecture: str
    accepted_mappings: frozenset[str]
    cardinality_ceiling: int = 10_000
    user_profile_env_var: str | None = None
    password_env_var: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "pyd_path",
            "installation_id",
            "profile_id",
            "expected_python_abi",
            "expected_architecture",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        if not isinstance(self.accepted_mappings, frozenset):
            raise TypeError("accepted_mappings must be a frozenset")
        if isinstance(self.cardinality_ceiling, bool) or not isinstance(
            self.cardinality_ceiling, int
        ):
            raise TypeError("cardinality_ceiling must be an integer")
        if self.cardinality_ceiling < 1:
            raise ValueError("cardinality_ceiling must be positive")


class NativePowerFactory2026Vendor:
    """Attached-session, read-only implementation of ``PowerFactory2026Vendor``."""

    def __init__(
        self,
        config: NativePowerFactory2026Config,
        *,
        module_loader: ModuleLoader | None = None,
        environ: Mapping[str, str] | None = None,
        id_factory: IdFactory | None = None,
    ) -> None:
        self._config = config
        self._module_loader = module_loader or _load_powerfactory_module
        self._environ = os.environ if environ is None else environ
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._module: ModuleType | None = None
        self._application: Any = None
        self._session_id: str | None = None
        self._prior_project: Any = None
        self._prior_study_case: Any = None
        self._activated_context = False
        self._executions: dict[str, Any] = {}
        self._logs: list[VendorLogRecord] = []

    def start(self, request: SessionStartRequest) -> VendorSession:
        if self._application is not None:
            raise InvalidOperation("native vendor session is already started")
        if not request.read_only:
            raise InvalidOperation("native PowerFactory 2026 vendor admits read-only sessions only")
        if request.requested_release != "2026":
            raise InvalidOperation("native vendor supports PowerFactory release 2026 only")
        if (request.installation_id, request.profile_id) != (
            self._config.installation_id,
            self._config.profile_id,
        ):
            raise ConfigurationMismatch(
                "session request does not match configured installation/profile"
            )
        actual_abi = sys.implementation.cache_tag or "unknown"
        actual_architecture = platform.machine()
        if actual_abi != self._config.expected_python_abi:
            raise ConfigurationMismatch(
                "running Python ABI does not match PowerFactory configuration"
            )
        if actual_architecture.casefold() != self._config.expected_architecture.casefold():
            raise ConfigurationMismatch(
                "running architecture does not match PowerFactory configuration"
            )
        module = self._module_loader(self._config.pyd_path)
        acquire = getattr(module, "GetApplicationExt", None)
        if not callable(acquire):
            raise NativeMappingUnavailable("GetApplicationExt is unavailable")
        profile = self._environment_value(self._config.user_profile_env_var)
        password = self._environment_value(self._config.password_env_var)
        try:
            application = acquire(profile, password, None)
        finally:
            password = None
        if application is None:
            raise InvalidOperation("GetApplicationExt returned no application")
        # The admitted release is validated before acquisition and bound to the
        # configured installation identity. PowerFactory 2026 SP1 does not
        # expose Application.GetVersion through its Python application object.
        version = request.requested_release
        self._module = module
        self._application = application
        self._session_id = self._id_factory()
        self._logs.append(
            VendorLogRecord(None, LogSeverity.INFO, "lifecycle", "native session attached")
        )
        return VendorSession(
            self._session_id,
            version,
            actual_abi,
            actual_architecture,
            tuple(sorted(self._config.accepted_mappings)),
        )

    def inspect_context(self) -> VendorContext:
        application = self._required_application()
        self._require_mapping("context.active")
        project = _required_call(application, "GetActiveProject")
        study_case = _required_call(application, "GetActiveStudyCase")
        scenario = _optional_call(application, "GetActiveScenario")
        if project is None or study_case is None:
            return VendorContext(None, None, None, (), ())
        stages = self._bounded(_required_call(application, "GetActiveStages"), "active stages")
        stage_records: list[VariantStageObservation] = []
        for stage in stages:
            parent = _required_call(stage, "GetParent")
            stage_records.append(
                VariantStageObservation(_object_key(parent), _object_key(stage), True)
            )
        grids = self._bounded(
            _required_call(application, "GetCalcRelevantObjects", "*.ElmNet", False),
            "active grids",
        )
        grid_keys = tuple(sorted({_object_key(item) for item in grids}))
        return VendorContext(
            _object_key(project),
            _object_key(study_case),
            None if scenario is None else _object_key(scenario),
            tuple(sorted(stage_records, key=lambda item: (item.variant_key, item.stage_key))),
            grid_keys,
        )

    def activate_context(self, request: Any) -> VendorContext:
        application = self._required_application()
        self._require_mapping("context.activate")
        if not self._activated_context:
            self._prior_project = _required_call(application, "GetActiveProject")
            self._prior_study_case = _required_call(application, "GetActiveStudyCase")
        user = _required_call(application, "GetCurrentUser")
        project = self._select_exact(
            self._bounded(_required_call(user, "GetContents", "*.IntPrj", 1), "projects"),
            request.project_key,
            "project",
        )
        result = _required_call(application, "ActivateProject", _object_key(project))
        _require_zero_status(result, "project activation")
        study_folder = _required_call(application, "GetProjectFolder", "study")
        study_case = self._select_exact(
            self._bounded(
                _required_call(study_folder, "GetContents", "*.IntCase", 1), "study cases"
            ),
            request.study_case_key,
            "study case",
        )
        active_study_case = _required_call(application, "GetActiveStudyCase")
        if active_study_case is None or _object_key(active_study_case) != _object_key(
            study_case
        ):
            _require_zero_status(
                _required_call(study_case, "Activate"), "study-case activation"
            )
        current_scenario = _optional_call(application, "GetActiveScenario")
        if request.operational_scenario_key is None:
            if current_scenario is not None:
                _require_zero_status(
                    _required_call(current_scenario, "Deactivate"), "scenario deactivation"
                )
        else:
            scenario_folder = _required_call(application, "GetProjectFolder", "scen")
            scenario = self._select_exact(
                self._bounded(
                    _required_call(scenario_folder, "GetContents", "*.IntScenario", 1),
                    "scenarios",
                ),
                request.operational_scenario_key,
                "scenario",
            )
            _require_zero_status(_required_call(scenario, "Activate"), "scenario activation")
        self._activated_context = True
        observed = self.inspect_context()
        if (
            observed.project_key != request.project_key
            or observed.study_case_key != request.study_case_key
            or observed.operational_scenario_key != request.operational_scenario_key
        ):
            raise ConfigurationMismatch("post-activation context verification failed")
        return observed

    def query_objects(self, request: ObjectQueryRequest) -> Sequence[VendorObjectRecord]:
        application = self._required_application()
        records: list[VendorObjectRecord] = []
        for selector in request.object_classes:
            native_class = self._native_class(selector.kind)
            if request.scope is ObjectQueryScope.ACTIVE_GRIDS:
                values = _required_call(
                    application, "GetCalcRelevantObjects", f"*.{native_class}", True
                )
            elif request.scope is ObjectQueryScope.ACTIVE_PROJECT:
                project = _required_call(application, "GetActiveProject")
                values = _required_call(project, "GetContents", f"*.{native_class}", 1)
            else:
                raise NativeMappingUnavailable("object query scope is unsupported")
            for value in self._bounded(values, f"{native_class} query"):
                self._assert_class(value, native_class)
                object_selector = self._selector(value, selector)
                fields = tuple(self._read_field(value, item) for item in request.attributes)
                records.append(
                    VendorObjectRecord(
                        object_selector,
                        _object_name(value),
                        fields,
                        self._out_of_service(value, selector.kind),
                    )
                )
        return tuple(sorted(records, key=lambda item: _selector_key(item.selector)))

    def observe_dependencies(
        self, request: DependencyReadRequest
    ) -> tuple[Sequence[VendorDependencyRecord], bool]:
        records: list[VendorDependencyRecord] = []
        for selector in request.objects:
            value = self._resolve(selector)
            fields = tuple(self._read_field(value, item) for item in request.attributes)
            relationships: list[RelationshipObservation] = []
            for relationship in request.relationships:
                if relationship.kind is not RelationshipKind.CONNECTED_TERMINAL:
                    raise NativeMappingUnavailable("requested relationship mapping is unsupported")
                self._require_mapping("relationship.connected_terminal")
                for target in self._connected_terminals(value, selector.object_class.kind):
                    target_class = type(selector.object_class)(
                        ObjectClassKind.TERMINAL, selector.object_class.contract
                    )
                    relationships.append(
                        RelationshipObservation(relationship, self._selector(target, target_class))
                    )
            records.append(
                VendorDependencyRecord(
                    selector,
                    fields,
                    tuple(sorted(relationships, key=lambda item: _selector_key(item.target))),
                )
            )
        return tuple(sorted(records, key=lambda item: _selector_key(item.selector))), True

    def execute_command(self, request: CommandExecutionRequest) -> VendorCommandOutcome:
        application = self._required_application()
        if request.command.kind is not CommandKind.LOAD_FLOW:
            raise NativeMappingUnavailable("only the admitted ComLdf command is implemented")
        self._require_mapping("command.load_flow")
        if request.settings:
            raise NativeMappingUnavailable(
                "native load-flow settings lack accepted mapping evidence"
            )
        command = _required_call(application, "GetFromStudyCase", "ComLdf")
        if command is None:
            raise ObjectNotFound("active study case has no ComLdf command")
        return_code = _required_call(command, "Execute")
        if isinstance(return_code, bool) or not isinstance(return_code, int):
            raise InvalidOperation("ComLdf Execute returned a non-integer status")
        execution_id = self._id_factory()
        self._executions[execution_id] = command
        self._logs.append(
            VendorLogRecord(
                execution_id,
                LogSeverity.INFO if return_code == 0 else LogSeverity.ERROR,
                "load_flow",
                "ComLdf completed"
                if return_code == 0
                else f"ComLdf failed with status {return_code}",
            )
        )
        return VendorCommandOutcome(execution_id, return_code, ())

    def collect_results(self, request: ResultCollectionRequest) -> Sequence[VendorResultRecord]:
        if request.execution_id not in self._executions:
            raise ObjectNotFound("native execution was not found")
        records: list[VendorResultRecord] = []
        for selector in request.objects:
            value = self._resolve(selector)
            cells = tuple(self._read_result(value, variable) for variable in request.variables)
            records.append(VendorResultRecord(selector, cells))
        return tuple(sorted(records, key=lambda item: _selector_key(item.selector)))

    def read_logs(self) -> Sequence[VendorLogRecord]:
        self._required_application()
        return tuple(self._logs)

    def close(self) -> Sequence[str]:
        if self._application is None:
            return ()
        diagnostics: list[str] = []
        try:
            if self._activated_context and self._prior_project is not None:
                _require_zero_status(
                    _required_call(self._prior_project, "Activate"), "project restoration"
                )
                diagnostics.append("prior project restored")
                if self._prior_study_case is not None:
                    _require_zero_status(
                        _required_call(self._prior_study_case, "Activate"),
                        "study-case restoration",
                    )
                    diagnostics.append("prior study case restored")
        finally:
            self._application = None
            self._module = None
            self._session_id = None
            self._prior_project = None
            self._prior_study_case = None
            self._activated_context = False
            self._executions.clear()
            self._logs.clear()
        return tuple(diagnostics)

    def _native_class(self, kind: ObjectClassKind) -> str:
        native = _CLASS_NAMES.get(kind)
        if native is None:
            raise NativeMappingUnavailable("object class mapping is unsupported")
        self._require_mapping(f"class.{kind.value}")
        return native

    def _read_field(self, value: Any, selector: Any) -> PrimitiveField:
        mapping = _ATTRIBUTES.get(selector.kind)
        if mapping is None:
            raise NativeMappingUnavailable("attribute mapping is unsupported")
        self._require_mapping(f"attribute.{selector.kind.value}")
        native_name, required_unit = mapping
        raw = _read_attribute(value, native_name)
        if selector.kind is AttributeKind.DISPLAY_NAME:
            if not isinstance(raw, str) or not raw:
                raise InvalidOperation("display-name attribute is unavailable")
            return PrimitiveField(selector, raw)
        numeric = _decimal(raw, native_name)
        unit = _attribute_unit(value, native_name)
        if unit != required_unit:
            raise NativeMappingUnavailable("native attribute unit does not match admitted mapping")
        return PrimitiveField(selector, Quantity(numeric, unit))

    def _read_result(self, value: Any, variable: Any) -> ResultCell:
        mapping = _RESULTS.get(variable.kind)
        if mapping is None:
            return ResultCell(
                variable, ResultCellStatus.UNSUPPORTED, None, None, None, "mapping unavailable"
            )
        self._require_mapping(f"result.{variable.kind.value}")
        native_name, required_unit = mapping
        try:
            raw = _read_attribute(value, native_name)
        except NativeMappingUnavailable:
            return ResultCell(
                variable, ResultCellStatus.MISSING, None, None, None, "result unavailable"
            )
        unit = _attribute_unit(value, native_name)
        if unit != required_unit:
            return ResultCell(
                variable, ResultCellStatus.UNSUPPORTED, None, None, None, "unit mapping unavailable"
            )
        try:
            numeric = _decimal(raw, native_name)
        except InvalidOperation:
            if (
                isinstance(raw, (int, float))
                and not isinstance(raw, bool)
                and not math.isfinite(float(raw))
            ):
                return ResultCell(
                    variable, ResultCellStatus.NON_FINITE, str(raw), unit, None, "non-finite result"
                )
            return ResultCell(
                variable, ResultCellStatus.MISSING, None, None, None, "result is not numeric"
            )
        return ResultCell(
            variable, ResultCellStatus.AVAILABLE, str(numeric), unit, Quantity(numeric, unit), None
        )

    def _resolve(self, selector: PrimitiveObjectSelector) -> Any:
        if selector.project_key != _object_key(
            _required_call(self._required_application(), "GetActiveProject")
        ):
            raise ConfigurationMismatch("selector project does not match active project")
        if selector.native_field is not None:
            raise NativeMappingUnavailable(
                "native locator fields are not accepted for PowerFactory 2026"
            )
        native_class = self._native_class(selector.object_class.kind)
        values = self._bounded(
            _required_call(
                self._required_application(), "GetCalcRelevantObjects", f"*.{native_class}", True
            ),
            "selector resolution",
        )
        matches = [item for item in values if selector.canonical_path == _object_key(item)]
        if not matches:
            raise ObjectNotFound("exact canonical locator has no match")
        if len(matches) > 1:
            raise ObjectAmbiguous("exact canonical locator is ambiguous")
        self._assert_class(matches[0], native_class)
        return matches[0]

    def _selector(self, value: Any, object_class: Any) -> PrimitiveObjectSelector:
        return PrimitiveObjectSelector(
            _object_key(_required_call(self._required_application(), "GetActiveProject")),
            object_class,
            None,
            None,
            _object_key(value),
        )

    def _connected_terminals(self, value: Any, kind: ObjectClassKind) -> tuple[Any, ...]:
        paths = _CONNECTED_TERMINAL_PATHS.get(kind)
        if paths is None:
            raise NativeMappingUnavailable(
                "connected-terminal mapping is unsupported for object class"
            )
        targets: list[Any] = []
        for path in paths:
            current = value
            for attribute in path:
                current = _read_attribute(current, attribute)
                if current is None:
                    raise NativeMappingUnavailable("connected-terminal relationship is unavailable")
            self._assert_class(current, "ElmTerm")
            targets.append(current)
        return tuple(targets)

    @staticmethod
    def _out_of_service(value: Any, kind: ObjectClassKind) -> bool:
        if kind is ObjectClassKind.GRID:
            return False
        raw = _read_attribute(value, "outserv")
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int) and raw in (0, 1):
            return bool(raw)
        raise NativeMappingUnavailable("outserv mapping returned unsupported evidence")

    @staticmethod
    def _assert_class(value: Any, expected: str) -> None:
        actual = _required_call(value, "GetClassName")
        if actual != expected:
            raise NativeMappingUnavailable("native object class does not match admitted mapping")

    def _bounded(self, values: Any, label: str) -> list[Any]:
        if values is None:
            return []
        if isinstance(values, (str, bytes, bytearray)):
            raise InvalidOperation(f"{label} returned a non-object sequence")
        try:
            result = list(values)
        except TypeError:
            raise InvalidOperation(f"{label} returned a non-sequence") from None
        if len(result) > self._config.cardinality_ceiling:
            raise InvalidOperation(f"{label} exceeded configured cardinality ceiling")
        return result

    @staticmethod
    def _select_exact(values: Sequence[Any], key: str, label: str) -> Any:
        matches = [item for item in values if key in {_object_key(item), _object_name(item)}]
        if not matches:
            raise ObjectNotFound(f"exact {label} selector has no match")
        if len(matches) > 1:
            raise ObjectAmbiguous(f"exact {label} selector is ambiguous")
        return matches[0]

    def _require_mapping(self, mapping: str) -> None:
        if mapping not in self._config.accepted_mappings:
            raise NativeMappingUnavailable(f"native mapping is not accepted: {mapping}")

    def _required_application(self) -> Any:
        if self._application is None:
            raise InvalidOperation("native vendor session is not started")
        return self._application

    def _environment_value(self, name: str | None) -> str | None:
        if name is None:
            return None
        value = self._environ.get(name)
        if value is None:
            raise ConfigurationMismatch(
                "required PowerFactory profile environment value is unavailable"
            )
        return value


def _load_powerfactory_module(path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location("powerfactory", path)
    if spec is None or spec.loader is None:
        raise InvalidOperation("powerfactory extension could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _required_call(target: Any, method_name: str, *args: Any) -> Any:
    method = getattr(target, method_name, None)
    if not callable(method):
        raise NativeMappingUnavailable(f"required native method is unavailable: {method_name}")
    try:
        return method(*args)
    except Exception:
        raise InvalidOperation(f"native method failed: {method_name}") from None


def _optional_call(target: Any, method_name: str) -> Any:
    method = getattr(target, method_name, None)
    if not callable(method):
        raise NativeMappingUnavailable(f"required native method is unavailable: {method_name}")
    try:
        return method()
    except Exception:
        raise InvalidOperation(f"native method failed: {method_name}") from None


def _read_attribute(value: Any, attribute: str) -> Any:
    method = getattr(value, "GetAttribute", None)
    if callable(method):
        try:
            return method(attribute)
        except Exception:
            raise NativeMappingUnavailable(
                f"native attribute is unavailable: {attribute}"
            ) from None
    try:
        return getattr(value, attribute)
    except Exception:
        raise NativeMappingUnavailable(f"native attribute is unavailable: {attribute}") from None


def _attribute_unit(value: Any, attribute: str) -> str:
    unit = _required_call(value, "GetAttributeUnit", attribute)
    if not isinstance(unit, str) or not unit:
        raise NativeMappingUnavailable("native attribute unit is unavailable")
    return unit


def _object_key(value: Any) -> str:
    key = _required_call(value, "GetFullName")
    if not isinstance(key, str) or not key:
        raise NativeMappingUnavailable("native object has no exact canonical locator")
    return key


def _object_name(value: Any) -> str:
    name = getattr(value, "loc_name", None)
    if not isinstance(name, str) or not name:
        raise NativeMappingUnavailable("native object has no display name")
    return name


def _selector_key(value: PrimitiveObjectSelector) -> tuple[str, str]:
    return value.object_class.kind.value, value.canonical_path or ""


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise InvalidOperation(f"{label} is not numeric")
    try:
        numeric = Decimal(str(value))
    except (DecimalInvalidOperation, ValueError):
        raise InvalidOperation(f"{label} is not numeric") from None
    if not numeric.is_finite():
        raise InvalidOperation(f"{label} is not finite")
    return numeric


def _require_zero_status(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != 0:
        raise InvalidOperation(f"{label} did not return success status")


__all__ = [
    "NativeMappingUnavailable",
    "NativePowerFactory2026Config",
    "NativePowerFactory2026Vendor",
]
