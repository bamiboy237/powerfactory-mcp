"""Serializable contracts for the narrow PowerFactory vendor boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .models import CommandSetting, PageCursor, VersionedName
from .values import (
    ConfigurationKey,
    LiveStateFingerprint,
    MAX_PAGE_SIZE,
    Quantity,
    require_aware,
    require_collection,
    require_text,
)


MAX_GATEWAY_WARNINGS = 16
MAX_LOG_BYTES = 65_536
PrimitiveValue = str | bool | int | Quantity


def _require_limit(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 1 or value > MAX_PAGE_SIZE:
        raise ValueError(f"{field_name} must be between 1 and {MAX_PAGE_SIZE}")


def _require_primitive(value: PrimitiveValue, field_name: str) -> None:
    if isinstance(value, str):
        require_text(value, field_name)
        return
    if isinstance(value, (bool, Quantity)):
        return
    if isinstance(value, int):
        return
    raise TypeError(f"{field_name} must be a serializable primitive value")


class GatewayReadiness(str, Enum):
    READY = "ready"
    NOT_READY = "not_ready"


class CommandCompletion(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class LogSeverity(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ObjectClassKind(str, Enum):
    GRID = "grid"
    TERMINAL = "terminal"
    LINE = "line"
    LOAD = "load"
    TRANSFORMER = "transformer"


class AttributeKind(str, Enum):
    DISPLAY_NAME = "display_name"
    NOMINAL_VOLTAGE = "nominal_voltage"
    ACTIVE_POWER = "active_power"
    REACTIVE_POWER = "reactive_power"


class RelationshipKind(str, Enum):
    CONNECTED_TERMINAL = "connected_terminal"


class ResultVariableKind(str, Enum):
    BUS_VOLTAGE = "bus_voltage"
    ACTIVE_POWER = "active_power"
    BUS_CURRENT = "bus_current"
    ROTOR_ANGLE = "rotor_angle"


class CommandKind(str, Enum):
    LOAD_FLOW = "load_flow"
    RMS_SIMULATION = "rms_simulation"


class ObjectQueryScope(str, Enum):
    ACTIVE_GRIDS = "active_grids"
    ACTIVE_PROJECT = "active_project"


class OutOfServicePolicy(str, Enum):
    EXCLUDE = "exclude"
    INCLUDE = "include"
    ONLY = "only"


class GatewayWarningCode(str, Enum):
    QUERY_TRUNCATED = "query_truncated"
    UNSUPPORTED_SELECTOR = "unsupported_selector"


class ResultCellStatus(str, Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    UNSUPPORTED = "unsupported"
    NON_FINITE = "non_finite"


class AttributeWriteDisposition(str, Enum):
    CONFIRMED = "confirmed"
    PRECONDITION_REJECTED = "precondition_rejected"
    EFFECT_UNCERTAIN = "effect_uncertain"


@dataclass(frozen=True, slots=True)
class ObjectClassSelector:
    kind: ObjectClassKind
    contract: VersionedName

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ObjectClassKind) or not isinstance(self.contract, VersionedName):
            raise TypeError("ObjectClassSelector requires ObjectClassKind and VersionedName")


@dataclass(frozen=True, slots=True)
class AttributeSelector:
    kind: AttributeKind
    contract: VersionedName

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AttributeKind) or not isinstance(self.contract, VersionedName):
            raise TypeError("AttributeSelector requires AttributeKind and VersionedName")


@dataclass(frozen=True, slots=True)
class RelationshipSelector:
    kind: RelationshipKind
    contract: VersionedName

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RelationshipKind) or not isinstance(self.contract, VersionedName):
            raise TypeError("RelationshipSelector requires RelationshipKind and VersionedName")


@dataclass(frozen=True, slots=True)
class ResultVariableSelector:
    kind: ResultVariableKind
    contract: VersionedName

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ResultVariableKind) or not isinstance(self.contract, VersionedName):
            raise TypeError("ResultVariableSelector requires ResultVariableKind and VersionedName")


@dataclass(frozen=True, slots=True)
class CommandSelector:
    kind: CommandKind
    contract: VersionedName

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CommandKind) or not isinstance(self.contract, VersionedName):
            raise TypeError("CommandSelector requires CommandKind and VersionedName")


@dataclass(frozen=True, slots=True)
class PrimitiveField:
    selector: AttributeSelector
    value: PrimitiveValue

    def __post_init__(self) -> None:
        _require_primitive(self.value, "PrimitiveField.value")


@dataclass(frozen=True, slots=True)
class PrimitiveObjectSelector:
    project_key: str
    object_class: ObjectClassSelector
    native_field: Optional[str]
    native_value: Optional[str]
    canonical_path: Optional[str]

    def __post_init__(self) -> None:
        require_text(self.project_key, "PrimitiveObjectSelector.project_key", maximum=512)
        if (self.native_field is None) != (self.native_value is None):
            raise ValueError("native_field and native_value must be present together")
        if self.native_field is not None:
            require_text(self.native_field, "PrimitiveObjectSelector.native_field", maximum=128)
            require_text(self.native_value, "PrimitiveObjectSelector.native_value")
        if self.canonical_path is not None:
            require_text(self.canonical_path, "PrimitiveObjectSelector.canonical_path")
        if self.native_value is None and self.canonical_path is None:
            raise ValueError("selector requires native evidence or a canonical path")


@dataclass(frozen=True, slots=True)
class SessionStartRequest:
    installation_id: str
    profile_id: str
    requested_release: str
    requested_service_pack: str
    read_only: bool

    def __post_init__(self) -> None:
        for name in ("installation_id", "profile_id", "requested_release", "requested_service_pack"):
            require_text(getattr(self, name), f"SessionStartRequest.{name}", maximum=256)
        if not isinstance(self.read_only, bool):
            raise TypeError("SessionStartRequest.read_only must be a boolean")


@dataclass(frozen=True, slots=True)
class SessionObservation:
    session_id: str
    adapter_version: str
    powerfactory_version: str
    python_abi: str
    architecture: str
    readiness: GatewayReadiness
    capabilities: Tuple[str, ...]
    started_at: datetime

    def __post_init__(self) -> None:
        for name in ("session_id", "adapter_version", "powerfactory_version", "python_abi", "architecture"):
            require_text(getattr(self, name), f"SessionObservation.{name}", maximum=256)
        require_collection(self.capabilities, "SessionObservation.capabilities")
        for capability in self.capabilities:
            require_text(capability, "SessionObservation.capabilities entry", maximum=128)
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("SessionObservation.capabilities must be unique")
        require_aware(self.started_at, "SessionObservation.started_at")


@dataclass(frozen=True, slots=True)
class VariantStageObservation:
    variant_key: str
    stage_key: str
    active: bool

    def __post_init__(self) -> None:
        require_text(self.variant_key, "VariantStageObservation.variant_key", maximum=512)
        require_text(self.stage_key, "VariantStageObservation.stage_key", maximum=512)
        if not isinstance(self.active, bool):
            raise TypeError("VariantStageObservation.active must be a boolean")


@dataclass(frozen=True, slots=True)
class ContextObservation:
    session_id: str
    project_key: Optional[str]
    study_case_key: Optional[str]
    operational_scenario_key: Optional[str]
    variant_stages: Tuple[VariantStageObservation, ...]
    active_grid_keys: Tuple[str, ...]
    configuration_key: Optional[ConfigurationKey]
    verified: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        require_text(self.session_id, "ContextObservation.session_id", maximum=256)
        for name in ("project_key", "study_case_key", "operational_scenario_key"):
            value = getattr(self, name)
            if value is not None:
                require_text(value, f"ContextObservation.{name}", maximum=512)
        require_collection(self.variant_stages, "ContextObservation.variant_stages")
        require_collection(self.active_grid_keys, "ContextObservation.active_grid_keys")
        for value in self.active_grid_keys:
            require_text(value, "ContextObservation.active_grid_keys entry", maximum=512)
        if len(set(self.active_grid_keys)) != len(self.active_grid_keys):
            raise ValueError("ContextObservation.active_grid_keys must be unique")
        if not isinstance(self.verified, bool):
            raise TypeError("ContextObservation.verified must be a boolean")
        if self.verified and (
            self.project_key is None
            or self.study_case_key is None
            or not self.active_grid_keys
            or self.configuration_key is None
        ):
            raise ValueError("verified context requires project, study case, active grids, and key")
        require_aware(self.observed_at, "ContextObservation.observed_at")


@dataclass(frozen=True, slots=True)
class ContextActivationRequest:
    project_key: str
    study_case_key: str
    operational_scenario_key: Optional[str]

    def __post_init__(self) -> None:
        require_text(self.project_key, "ContextActivationRequest.project_key", maximum=512)
        require_text(self.study_case_key, "ContextActivationRequest.study_case_key", maximum=512)
        if self.operational_scenario_key is not None:
            require_text(self.operational_scenario_key, "ContextActivationRequest.operational_scenario_key", maximum=512)


@dataclass(frozen=True, slots=True)
class ContextActivationObservation:
    requested: ContextActivationRequest
    context: ContextObservation
    changed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.changed, bool):
            raise TypeError("ContextActivationObservation.changed must be a boolean")
        if not self.context.verified:
            raise ValueError("activation observation requires a verified context")
        if (
            self.context.project_key != self.requested.project_key
            or self.context.study_case_key != self.requested.study_case_key
            or self.context.operational_scenario_key != self.requested.operational_scenario_key
        ):
            raise ValueError("activation observation must match the requested context")


@dataclass(frozen=True, slots=True)
class GatewayWarning:
    code: GatewayWarningCode
    message: str
    count: int

    def __post_init__(self) -> None:
        require_text(self.message, "GatewayWarning.message")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 1:
            raise ValueError("GatewayWarning.count must be a positive integer")


@dataclass(frozen=True, slots=True)
class ObjectQueryRequest:
    configuration_key: ConfigurationKey
    scope: ObjectQueryScope
    out_of_service: OutOfServicePolicy
    object_classes: Tuple[ObjectClassSelector, ...]
    attributes: Tuple[AttributeSelector, ...]
    limit: int
    cursor: Optional[PageCursor]

    def __post_init__(self) -> None:
        require_collection(self.object_classes, "ObjectQueryRequest.object_classes", allow_empty=False)
        require_collection(self.attributes, "ObjectQueryRequest.attributes")
        if len(set(self.object_classes)) != len(self.object_classes):
            raise ValueError("ObjectQueryRequest.object_classes must be unique")
        if len(set(self.attributes)) != len(self.attributes):
            raise ValueError("ObjectQueryRequest.attributes must be unique")
        _require_limit(self.limit, "ObjectQueryRequest.limit")


@dataclass(frozen=True, slots=True)
class ObjectObservation:
    selector: PrimitiveObjectSelector
    display_name: str
    fields: Tuple[PrimitiveField, ...]

    def __post_init__(self) -> None:
        require_text(self.display_name, "ObjectObservation.display_name")
        require_collection(self.fields, "ObjectObservation.fields")
        selectors = tuple(field.selector for field in self.fields)
        if len(set(selectors)) != len(selectors):
            raise ValueError("ObjectObservation.fields must have unique selectors")


@dataclass(frozen=True, slots=True)
class ObjectQueryBatch:
    configuration_key: ConfigurationKey
    records: Tuple[ObjectObservation, ...]
    next_cursor: Optional[PageCursor]
    complete: bool
    truncated: bool
    warnings: Tuple[GatewayWarning, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        require_collection(self.records, "ObjectQueryBatch.records")
        require_collection(self.warnings, "ObjectQueryBatch.warnings")
        if len(self.warnings) > MAX_GATEWAY_WARNINGS:
            raise ValueError(f"ObjectQueryBatch.warnings exceeds {MAX_GATEWAY_WARNINGS} entries")
        if not isinstance(self.complete, bool) or not isinstance(self.truncated, bool):
            raise TypeError("object query completion flags must be booleans")
        if self.complete == self.truncated:
            raise ValueError("exactly one of complete or truncated must be true")
        if self.truncated != (self.next_cursor is not None):
            raise ValueError("truncated batches require a continuation cursor")
        require_aware(self.observed_at, "ObjectQueryBatch.observed_at")


@dataclass(frozen=True, slots=True)
class DependencyReadRequest:
    configuration_key: ConfigurationKey
    objects: Tuple[PrimitiveObjectSelector, ...]
    attributes: Tuple[AttributeSelector, ...]
    relationships: Tuple[RelationshipSelector, ...]
    limit: int

    def __post_init__(self) -> None:
        require_collection(self.objects, "DependencyReadRequest.objects", allow_empty=False)
        require_collection(self.attributes, "DependencyReadRequest.attributes")
        require_collection(self.relationships, "DependencyReadRequest.relationships")
        if len(self.objects) > MAX_PAGE_SIZE:
            raise ValueError(f"DependencyReadRequest.objects exceeds {MAX_PAGE_SIZE} entries")
        if len(set(self.attributes)) != len(self.attributes) or len(set(self.relationships)) != len(self.relationships):
            raise ValueError("dependency selectors must be unique")
        _require_limit(self.limit, "DependencyReadRequest.limit")


@dataclass(frozen=True, slots=True)
class RelationshipObservation:
    selector: RelationshipSelector
    target: PrimitiveObjectSelector


@dataclass(frozen=True, slots=True)
class ObjectDependencyObservation:
    selector: PrimitiveObjectSelector
    fields: Tuple[PrimitiveField, ...]
    relationships: Tuple[RelationshipObservation, ...]

    def __post_init__(self) -> None:
        require_collection(self.fields, "ObjectDependencyObservation.fields")
        require_collection(self.relationships, "ObjectDependencyObservation.relationships")


@dataclass(frozen=True, slots=True)
class DependencyObservation:
    configuration_key: ConfigurationKey
    objects: Tuple[ObjectDependencyObservation, ...]
    complete: bool
    fingerprint: LiveStateFingerprint
    observed_at: datetime

    def __post_init__(self) -> None:
        require_collection(self.objects, "DependencyObservation.objects")
        if not isinstance(self.complete, bool):
            raise TypeError("DependencyObservation.complete must be a boolean")
        require_aware(self.observed_at, "DependencyObservation.observed_at")


@dataclass(frozen=True, slots=True)
class CommandExecutionRequest:
    configuration_key: ConfigurationKey
    command: CommandSelector
    settings: Tuple[CommandSetting, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        require_collection(self.settings, "CommandExecutionRequest.settings")
        require_text(self.idempotency_key, "CommandExecutionRequest.idempotency_key", maximum=256)
        names = tuple(setting.name for setting in self.settings)
        if len(set(names)) != len(names):
            raise ValueError("CommandExecutionRequest.settings must have unique names")


@dataclass(frozen=True, slots=True)
class CommandExecutionObservation:
    execution_id: str
    command: CommandSelector
    completion: CommandCompletion
    return_code: int
    diagnostic_messages: Tuple[str, ...]
    started_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        require_text(self.execution_id, "CommandExecutionObservation.execution_id", maximum=256)
        if isinstance(self.return_code, bool) or not isinstance(self.return_code, int):
            raise TypeError("CommandExecutionObservation.return_code must be an integer")
        require_collection(self.diagnostic_messages, "CommandExecutionObservation.diagnostic_messages")
        for value in self.diagnostic_messages:
            require_text(value, "CommandExecutionObservation.diagnostic_messages entry")
        require_aware(self.started_at, "CommandExecutionObservation.started_at")
        require_aware(self.completed_at, "CommandExecutionObservation.completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("CommandExecutionObservation.completed_at cannot precede started_at")
        if (self.return_code == 0) != (self.completion is CommandCompletion.SUCCEEDED):
            raise ValueError("command completion must agree with its return code")


@dataclass(frozen=True, slots=True)
class ResultCollectionRequest:
    configuration_key: ConfigurationKey
    execution_id: str
    objects: Tuple[PrimitiveObjectSelector, ...]
    variables: Tuple[ResultVariableSelector, ...]
    limit: int
    cursor: Optional[PageCursor]

    def __post_init__(self) -> None:
        require_text(self.execution_id, "ResultCollectionRequest.execution_id", maximum=256)
        require_collection(self.objects, "ResultCollectionRequest.objects", allow_empty=False)
        require_collection(self.variables, "ResultCollectionRequest.variables", allow_empty=False)
        if len(self.objects) > MAX_PAGE_SIZE:
            raise ValueError(f"ResultCollectionRequest.objects exceeds {MAX_PAGE_SIZE} entries")
        if len(set(self.variables)) != len(self.variables):
            raise ValueError("ResultCollectionRequest.variables must be unique")
        _require_limit(self.limit, "ResultCollectionRequest.limit")


@dataclass(frozen=True, slots=True)
class ResultCell:
    variable: ResultVariableSelector
    status: ResultCellStatus
    source_value: Optional[str]
    source_unit: Optional[str]
    normalized: Optional[Quantity]
    diagnostic: Optional[str]

    def __post_init__(self) -> None:
        for name in ("source_value", "source_unit", "diagnostic"):
            value = getattr(self, name)
            if value is not None:
                require_text(value, f"ResultCell.{name}")
        if self.status is ResultCellStatus.AVAILABLE:
            if self.source_value is None or self.source_unit is None or self.normalized is None:
                raise ValueError("available result cells require source and normalized values")
        elif self.status is ResultCellStatus.NON_FINITE:
            if self.source_value is None or self.source_unit is None or self.normalized is not None:
                raise ValueError("non-finite cells preserve source evidence without a normalized value")
        elif self.source_value is not None or self.source_unit is not None or self.normalized is not None:
            raise ValueError("missing and unsupported cells cannot claim source or normalized values")


@dataclass(frozen=True, slots=True)
class ResultRow:
    selector: PrimitiveObjectSelector
    cells: Tuple[ResultCell, ...]

    def __post_init__(self) -> None:
        require_collection(self.cells, "ResultRow.cells")
        variables = tuple(cell.variable for cell in self.cells)
        if len(set(variables)) != len(variables):
            raise ValueError("ResultRow.cells must have unique variables")


@dataclass(frozen=True, slots=True)
class ResultBatch:
    execution_id: str
    rows: Tuple[ResultRow, ...]
    next_cursor: Optional[PageCursor]
    complete: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        require_text(self.execution_id, "ResultBatch.execution_id", maximum=256)
        require_collection(self.rows, "ResultBatch.rows")
        if not isinstance(self.complete, bool):
            raise TypeError("ResultBatch.complete must be a boolean")
        require_aware(self.observed_at, "ResultBatch.observed_at")


@dataclass(frozen=True, slots=True)
class LogReadRequest:
    execution_id: Optional[str]
    entry_limit: int
    byte_limit: int
    cursor: Optional[PageCursor]

    def __post_init__(self) -> None:
        if self.execution_id is not None:
            require_text(self.execution_id, "LogReadRequest.execution_id", maximum=256)
        _require_limit(self.entry_limit, "LogReadRequest.entry_limit")
        if isinstance(self.byte_limit, bool) or not isinstance(self.byte_limit, int):
            raise TypeError("LogReadRequest.byte_limit must be an integer")
        if self.byte_limit < 1 or self.byte_limit > MAX_LOG_BYTES:
            raise ValueError(f"LogReadRequest.byte_limit must be between 1 and {MAX_LOG_BYTES}")


@dataclass(frozen=True, slots=True)
class LogEntry:
    sequence: int
    execution_id: Optional[str]
    severity: LogSeverity
    category: str
    message: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("LogEntry.sequence must be a nonnegative integer")
        if self.execution_id is not None:
            require_text(self.execution_id, "LogEntry.execution_id", maximum=256)
        require_text(self.category, "LogEntry.category", maximum=128)
        require_text(self.message, "LogEntry.message")
        require_aware(self.observed_at, "LogEntry.observed_at")


@dataclass(frozen=True, slots=True)
class LogBatch:
    entries: Tuple[LogEntry, ...]
    next_cursor: Optional[PageCursor]
    bytes_returned: int
    truncated: bool
    redaction_applied: bool

    def __post_init__(self) -> None:
        require_collection(self.entries, "LogBatch.entries")
        if isinstance(self.bytes_returned, bool) or not isinstance(self.bytes_returned, int) or self.bytes_returned < 0:
            raise ValueError("LogBatch.bytes_returned must be a nonnegative integer")
        if not isinstance(self.truncated, bool) or not isinstance(self.redaction_applied, bool):
            raise TypeError("log batch flags must be booleans")
        if self.truncated != (self.next_cursor is not None):
            raise ValueError("truncated log batches require a continuation cursor")


@dataclass(frozen=True, slots=True)
class AttributeWriteRequest:
    configuration_key: ConfigurationKey
    selector: PrimitiveObjectSelector
    attribute: AttributeSelector
    expected_before: PrimitiveValue
    proposed: PrimitiveValue
    operation_id: str

    def __post_init__(self) -> None:
        _require_primitive(self.expected_before, "AttributeWriteRequest.expected_before")
        _require_primitive(self.proposed, "AttributeWriteRequest.proposed")
        require_text(self.operation_id, "AttributeWriteRequest.operation_id", maximum=256)
        if isinstance(self.expected_before, Quantity) != isinstance(self.proposed, Quantity):
            raise ValueError("quantity writes require quantities on both sides")
        if isinstance(self.expected_before, Quantity) and self.expected_before.unit != self.proposed.unit:
            raise ValueError("quantity write units must match")


@dataclass(frozen=True, slots=True)
class AttributeWriteObservation:
    operation_id: str
    selector: PrimitiveObjectSelector
    attribute: AttributeSelector
    observed_before: PrimitiveValue
    proposed: PrimitiveValue
    confirmed: Optional[PrimitiveValue]
    disposition: AttributeWriteDisposition
    observed_at: datetime

    def __post_init__(self) -> None:
        require_text(self.operation_id, "AttributeWriteObservation.operation_id", maximum=256)
        _require_primitive(self.observed_before, "AttributeWriteObservation.observed_before")
        _require_primitive(self.proposed, "AttributeWriteObservation.proposed")
        if self.confirmed is not None:
            _require_primitive(self.confirmed, "AttributeWriteObservation.confirmed")
        require_aware(self.observed_at, "AttributeWriteObservation.observed_at")
        if self.disposition is AttributeWriteDisposition.CONFIRMED and self.confirmed != self.proposed:
            raise ValueError("confirmed writes require matching readback")
        if self.disposition is AttributeWriteDisposition.PRECONDITION_REJECTED and self.confirmed != self.observed_before:
            raise ValueError("precondition rejection must confirm the unchanged observed value")
        if self.disposition is AttributeWriteDisposition.EFFECT_UNCERTAIN and self.confirmed is not None:
            raise ValueError("effect-uncertain writes cannot claim a confirmed value")


@dataclass(frozen=True, slots=True)
class CleanupObservation:
    session_id: Optional[str]
    was_open: bool
    cleanup_succeeded: bool
    diagnostic_messages: Tuple[str, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.session_id is not None:
            require_text(self.session_id, "CleanupObservation.session_id", maximum=256)
        if not isinstance(self.was_open, bool) or not isinstance(self.cleanup_succeeded, bool):
            raise TypeError("cleanup flags must be booleans")
        require_collection(self.diagnostic_messages, "CleanupObservation.diagnostic_messages")
        for message in self.diagnostic_messages:
            require_text(message, "CleanupObservation.diagnostic_messages entry")
        require_aware(self.observed_at, "CleanupObservation.observed_at")


__all__ = [name for name in globals() if not name.startswith("_") and name != "PrimitiveValue"] + ["PrimitiveValue"]
