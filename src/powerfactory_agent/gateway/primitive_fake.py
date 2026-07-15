"""Deterministic fake for the narrow PowerFactory vendor protocol."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
from typing import Callable

from powerfactory_agent.domain import (
    AttributeKind,
    AttributeSelector,
    AttributeWriteDisposition,
    AttributeWriteObservation,
    AttributeWriteRequest,
    CleanupObservation,
    CommandCompletion,
    CommandExecutionObservation,
    CommandExecutionRequest,
    CommandKind,
    CommandSelector,
    ConfigurationKey,
    ContextActivationObservation,
    ContextActivationRequest,
    ContextObservation,
    DependencyObservation,
    DependencyReadRequest,
    GatewayReadiness,
    GatewayWarning,
    GatewayWarningCode,
    LiveStateFingerprint,
    LogBatch,
    LogEntry,
    LogReadRequest,
    LogSeverity,
    ObjectClassKind,
    ObjectClassSelector,
    ObjectDependencyObservation,
    ObjectObservation,
    ObjectQueryBatch,
    ObjectQueryRequest,
    OutOfServicePolicy,
    PageCursor,
    PrimitiveField,
    PrimitiveObjectSelector,
    Quantity,
    RelationshipKind,
    RelationshipObservation,
    RelationshipSelector,
    ResultBatch,
    ResultCell,
    ResultCellStatus,
    ResultCollectionRequest,
    ResultRow,
    ResultVariableKind,
    ResultVariableSelector,
    SessionObservation,
    SessionStartRequest,
    VariantStageObservation,
    VersionedName,
)
from powerfactory_agent.serialization import canonical_digest, canonical_json

from .errors import ConfigurationMismatch, CursorInvalid, InvalidOperation, ObjectNotFound


_FIXED_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
_CURSOR_SECRET = b"primitive-gateway-contract-cursor-v1"
_SESSION_ID = "primitive-session-2026-07-14"
_OBJECT_CONTRACT = VersionedName("gateway-object-class", "v1")
_ATTRIBUTE_CONTRACT = VersionedName("gateway-attribute", "v1")
_RELATIONSHIP_CONTRACT = VersionedName("gateway-relationship", "v1")
_RESULT_CONTRACT = VersionedName("gateway-result-variable", "v1")
_COMMAND_CONTRACT = VersionedName("gateway-command", "v1")


def _quantity(value: str, unit: str) -> Quantity:
    return Quantity(Decimal(value), unit)


class DeterministicPrimitiveGateway:
    """Side-effect-free primitive fake with explicit lifecycle and bounded reads."""

    def __init__(
        self,
        *,
        allow_test_writes: bool = False,
        inject_effect_uncertain: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(allow_test_writes, bool) or not isinstance(inject_effect_uncertain, bool):
            raise TypeError("write controls must be booleans")
        self._allow_test_writes = allow_test_writes
        self._inject_effect_uncertain = inject_effect_uncertain
        self._clock = clock or (lambda: _FIXED_NOW)
        self._start_request: SessionStartRequest | None = None
        self._session: SessionObservation | None = None
        self._context: ContextObservation | None = None
        self._executions: dict[str, tuple[CommandExecutionRequest, CommandExecutionObservation]] = {}
        self._execution_sequence = 0
        self._logs: list[LogEntry] = []

        self.grid_class = ObjectClassSelector(ObjectClassKind.GRID, _OBJECT_CONTRACT)
        self.terminal_class = ObjectClassSelector(ObjectClassKind.TERMINAL, _OBJECT_CONTRACT)
        self.load_class = ObjectClassSelector(ObjectClassKind.LOAD, _OBJECT_CONTRACT)
        self.display_name_attribute = AttributeSelector(AttributeKind.DISPLAY_NAME, _ATTRIBUTE_CONTRACT)
        self.nominal_voltage_attribute = AttributeSelector(AttributeKind.NOMINAL_VOLTAGE, _ATTRIBUTE_CONTRACT)
        self.active_power_attribute = AttributeSelector(AttributeKind.ACTIVE_POWER, _ATTRIBUTE_CONTRACT)
        self.reactive_power_attribute = AttributeSelector(AttributeKind.REACTIVE_POWER, _ATTRIBUTE_CONTRACT)
        self.connected_terminal_relationship = RelationshipSelector(
            RelationshipKind.CONNECTED_TERMINAL,
            _RELATIONSHIP_CONTRACT,
        )
        self.load_flow_command = CommandSelector(CommandKind.LOAD_FLOW, _COMMAND_CONTRACT)
        self.rms_command = CommandSelector(CommandKind.RMS_SIMULATION, _COMMAND_CONTRACT)
        self.bus_voltage_result = ResultVariableSelector(ResultVariableKind.BUS_VOLTAGE, _RESULT_CONTRACT)
        self.active_power_result = ResultVariableSelector(ResultVariableKind.ACTIVE_POWER, _RESULT_CONTRACT)
        self.bus_current_result = ResultVariableSelector(ResultVariableKind.BUS_CURRENT, _RESULT_CONTRACT)
        self.rotor_angle_result = ResultVariableSelector(ResultVariableKind.ROTOR_ANGLE, _RESULT_CONTRACT)

        self.grid = PrimitiveObjectSelector("project-fixture", self.grid_class, "for_name", "Grid", "Grid.ElmNet")
        self.bus = PrimitiveObjectSelector(
            "project-fixture", self.terminal_class, "for_name", "North Bus", "Grid/North Bus.ElmTerm"
        )
        self.load = PrimitiveObjectSelector(
            "project-fixture", self.load_class, "for_name", "North Load", "Grid/North Load.ElmLod"
        )
        self._objects = (self.grid, self.bus, self.load)
        self._display_names = {self.grid: "Grid", self.bus: "North Bus", self.load: "North Load"}
        self._fields: dict[PrimitiveObjectSelector, dict[AttributeSelector, object]] = {
            self.grid: {self.display_name_attribute: "Grid"},
            self.bus: {
                self.display_name_attribute: "North Bus",
                self.nominal_voltage_attribute: _quantity("110", "kV"),
            },
            self.load: {
                self.display_name_attribute: "North Load",
                self.active_power_attribute: _quantity("10", "MW"),
                self.reactive_power_attribute: _quantity("3", "Mvar"),
            },
        }
        self._relationships = {self.load: {self.connected_terminal_relationship: self.bus}}
        self._result_values = {
            self.bus: {
                self.bus_voltage_result: ("1.010000", "p.u.", _quantity("1.01", "p.u.")),
                self.bus_current_result: ("nan", "A", None),
            },
            self.load: {
                self.active_power_result: ("10.000000", "MW", _quantity("10", "MW")),
            },
        }

    @property
    def started(self) -> bool:
        return self._session is not None

    def start(self, request: SessionStartRequest) -> SessionObservation:
        if self._session is not None:
            if request != self._start_request:
                raise InvalidOperation("gateway is already started with a different session request")
            return self._session
        if request.requested_release != "2026":
            raise InvalidOperation("requested PowerFactory release is unsupported")
        self._start_request = request
        self._session = SessionObservation(
            _SESSION_ID,
            "primitive-fake/0.2.0",
            "2026 SP0",
            "cp313",
            "x86_64",
            GatewayReadiness.READY,
            (
                "activate_context",
                "collect_results",
                "execute_command",
                "observe_dependencies",
                "query_objects",
                "read_logs",
            ),
            self._now(),
        )
        self._append_log(LogSeverity.INFO, "lifecycle", "primitive gateway session started")
        return self._session

    def inspect_context(self) -> ContextObservation:
        session = self._require_started()
        if self._context is not None:
            return self._context
        return ContextObservation(session.session_id, None, None, None, (), (), None, False, self._now())

    def activate_context(self, request: ContextActivationRequest) -> ContextActivationObservation:
        session = self._require_started()
        if request.project_key != "project-fixture" or request.study_case_key != "study-fixture":
            raise ObjectNotFound("requested project or study case was not found")
        prior = self._context
        variant_stages = (VariantStageObservation("variant-fixture", "stage-fixture", True),)
        configuration_key = ConfigurationKey(
            canonical_digest(
                {
                    "adapter": "primitive-fake/0.2.0",
                    "active_grids": ["grid-fixture"],
                    "operational_scenario": request.operational_scenario_key,
                    "profile_id": self._start_request.profile_id,
                    "project": request.project_key,
                    "study_case": request.study_case_key,
                    "variant_stages": variant_stages,
                },
                kind="configuration-key",
            )
        )
        context = ContextObservation(
            session.session_id,
            request.project_key,
            request.study_case_key,
            request.operational_scenario_key,
            variant_stages,
            ("grid-fixture",),
            configuration_key,
            True,
            self._now(),
        )
        self._context = context
        changed = prior is None or self._context_signature(prior) != self._context_signature(context)
        if changed:
            self._append_log(LogSeverity.INFO, "context", "active context verified")
        return ContextActivationObservation(request, context, changed)

    def query_objects(self, request: ObjectQueryRequest) -> ObjectQueryBatch:
        self._require_configuration(request.configuration_key)
        binding = canonical_digest(
            {
                "configuration_key": request.configuration_key,
                "scope": request.scope,
                "out_of_service": request.out_of_service,
                "object_classes": request.object_classes,
                "attributes": request.attributes,
            },
            kind="cursor-binding",
        )
        offset = self._decode_cursor(request.cursor, "objects", binding)
        admitted_classes = tuple(item for item in request.object_classes if item.contract == _OBJECT_CONTRACT)
        unsupported_count = len(request.object_classes) - len(admitted_classes)
        selected = tuple(item for item in self._objects if item.object_class in admitted_classes)
        if request.out_of_service is OutOfServicePolicy.ONLY:
            selected = ()
        page = selected[offset : offset + request.limit]
        records = tuple(
            ObjectObservation(item, self._display_names[item], self._select_fields(item, request.attributes))
            for item in page
        )
        next_offset = offset + len(page)
        truncated = next_offset < len(selected)
        next_cursor = self._encode_cursor("objects", binding, next_offset) if truncated else None
        warnings: list[GatewayWarning] = []
        if unsupported_count:
            warnings.append(
                GatewayWarning(
                    GatewayWarningCode.UNSUPPORTED_SELECTOR,
                    "one or more object-class selector versions are unsupported",
                    unsupported_count,
                )
            )
        if truncated:
            warnings.append(
                GatewayWarning(
                    GatewayWarningCode.QUERY_TRUNCATED,
                    "object query was truncated at the requested entry limit",
                    len(selected) - next_offset,
                )
            )
        return ObjectQueryBatch(
            request.configuration_key,
            records,
            next_cursor,
            not truncated,
            truncated,
            tuple(warnings),
            self._now(),
        )

    def observe_dependencies(self, request: DependencyReadRequest) -> DependencyObservation:
        self._require_configuration(request.configuration_key)
        if len(request.objects) > request.limit:
            raise InvalidOperation("dependency request exceeds its declared result limit")
        observations: list[ObjectDependencyObservation] = []
        complete = True
        for selector in request.objects:
            self._require_object(selector)
            fields = self._select_fields(selector, request.attributes)
            if len(fields) != len(request.attributes):
                complete = False
            available_relationships = self._relationships.get(selector, {})
            relationships = tuple(
                RelationshipObservation(relationship, available_relationships[relationship])
                for relationship in request.relationships
                if relationship in available_relationships
            )
            if len(relationships) != len(request.relationships):
                complete = False
            observations.append(ObjectDependencyObservation(selector, fields, relationships))
        objects = tuple(observations)
        fingerprint = LiveStateFingerprint(
            canonical_digest(
                {
                    "configuration_key": request.configuration_key,
                    "objects": objects,
                    "complete": complete,
                },
                kind="live-state-fingerprint",
            )
        )
        return DependencyObservation(request.configuration_key, objects, complete, fingerprint, self._now())

    def execute_command(self, request: CommandExecutionRequest) -> CommandExecutionObservation:
        self._require_configuration(request.configuration_key)
        for original, observation in self._executions.values():
            if original.idempotency_key == request.idempotency_key:
                if original != request:
                    raise InvalidOperation("idempotency key is already bound to a different command")
                return observation
        self._execution_sequence += 1
        execution_id = f"primitive-execution-{self._execution_sequence}"
        succeeded = request.command == self.load_flow_command
        observation = CommandExecutionObservation(
            execution_id,
            request.command,
            CommandCompletion.SUCCEEDED if succeeded else CommandCompletion.FAILED,
            0 if succeeded else 1,
            () if succeeded else ("command selector is unsupported by the fixture",),
            self._now(),
            self._now(),
        )
        self._executions[execution_id] = (request, observation)
        self._append_log(
            LogSeverity.INFO if succeeded else LogSeverity.ERROR,
            "command",
            "command completed" if succeeded else "command failed",
            execution_id=execution_id,
        )
        return observation

    def collect_results(self, request: ResultCollectionRequest) -> ResultBatch:
        self._require_configuration(request.configuration_key)
        stored = self._executions.get(request.execution_id)
        if stored is None:
            raise ObjectNotFound("command execution was not found")
        if stored[1].completion is not CommandCompletion.SUCCEEDED:
            raise InvalidOperation("results are unavailable for a failed command")
        binding = canonical_digest(
            {
                "configuration_key": request.configuration_key,
                "execution_id": request.execution_id,
                "objects": request.objects,
                "variables": request.variables,
            },
            kind="cursor-binding",
        )
        offset = self._decode_cursor(request.cursor, "results", binding)
        for selector in request.objects:
            self._require_object(selector)
        selected = request.objects[offset : offset + request.limit]
        rows = tuple(
            ResultRow(selector, tuple(self._result_cell(selector, variable) for variable in request.variables))
            for selector in selected
        )
        next_offset = offset + len(selected)
        next_cursor = self._encode_cursor("results", binding, next_offset) if next_offset < len(request.objects) else None
        return ResultBatch(request.execution_id, rows, next_cursor, next_cursor is None, self._now())

    def read_logs(self, request: LogReadRequest) -> LogBatch:
        self._require_started()
        binding = canonical_digest({"execution_id": request.execution_id}, kind="cursor-binding")
        offset = self._decode_cursor(request.cursor, "logs", binding)
        entries = tuple(self._logs)
        if request.execution_id is not None:
            if request.execution_id not in self._executions:
                raise ObjectNotFound("command execution was not found")
            entries = tuple(item for item in entries if item.execution_id == request.execution_id)
        page: list[LogEntry] = []
        byte_count = 0
        for entry in entries[offset : offset + request.entry_limit]:
            entry_bytes = len(canonical_json(entry).encode("utf-8"))
            if byte_count + entry_bytes > request.byte_limit:
                break
            page.append(entry)
            byte_count += entry_bytes
        next_offset = offset + len(page)
        truncated = next_offset < len(entries)
        next_cursor = self._encode_cursor("logs", binding, next_offset) if truncated else None
        return LogBatch(tuple(page), next_cursor, byte_count, truncated, False)

    def write_attribute(self, request: AttributeWriteRequest) -> AttributeWriteObservation:
        self._require_configuration(request.configuration_key)
        if not self._allow_test_writes or self._start_request.read_only:
            raise InvalidOperation("primitive mutation is disabled")
        self._require_object(request.selector)
        fields = self._fields[request.selector]
        if request.attribute not in fields:
            raise ObjectNotFound("requested attribute selector was not found")
        before = fields[request.attribute]
        if before != request.expected_before:
            return AttributeWriteObservation(
                request.operation_id,
                request.selector,
                request.attribute,
                before,
                request.proposed,
                before,
                AttributeWriteDisposition.PRECONDITION_REJECTED,
                self._now(),
            )
        fields[request.attribute] = request.proposed
        if self._inject_effect_uncertain:
            return AttributeWriteObservation(
                request.operation_id,
                request.selector,
                request.attribute,
                before,
                request.proposed,
                None,
                AttributeWriteDisposition.EFFECT_UNCERTAIN,
                self._now(),
            )
        confirmed = fields[request.attribute]
        return AttributeWriteObservation(
            request.operation_id,
            request.selector,
            request.attribute,
            before,
            request.proposed,
            confirmed,
            AttributeWriteDisposition.CONFIRMED,
            self._now(),
        )

    def close(self) -> CleanupObservation:
        session_id = self._session.session_id if self._session is not None else None
        was_open = self._session is not None
        self._session = None
        self._start_request = None
        self._context = None
        self._executions.clear()
        self._logs.clear()
        return CleanupObservation(session_id, was_open, True, (), self._now())

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _require_started(self) -> SessionObservation:
        if self._session is None:
            raise InvalidOperation("gateway session has not been started")
        return self._session

    def _require_configuration(self, configuration_key: ConfigurationKey) -> ContextObservation:
        self._require_started()
        if self._context is None or not self._context.verified:
            raise InvalidOperation("gateway context has not been activated and verified")
        if configuration_key != self._context.configuration_key:
            raise ConfigurationMismatch("configuration key does not match the active context")
        return self._context

    def _require_object(self, selector: PrimitiveObjectSelector) -> None:
        if selector not in self._fields:
            raise ObjectNotFound("object selector did not resolve exactly once")

    def _select_fields(
        self,
        selector: PrimitiveObjectSelector,
        attributes: tuple[AttributeSelector, ...],
    ) -> tuple[PrimitiveField, ...]:
        available = self._fields[selector]
        return tuple(PrimitiveField(attribute, available[attribute]) for attribute in attributes if attribute in available)

    def _result_cell(self, selector: PrimitiveObjectSelector, variable: ResultVariableSelector) -> ResultCell:
        if variable.contract != _RESULT_CONTRACT or variable.kind is ResultVariableKind.ROTOR_ANGLE:
            return ResultCell(variable, ResultCellStatus.UNSUPPORTED, None, None, None, "result selector is unsupported")
        evidence = self._result_values.get(selector, {}).get(variable)
        if evidence is None:
            return ResultCell(variable, ResultCellStatus.MISSING, None, None, None, "result value is absent")
        source_value, source_unit, normalized = evidence
        if normalized is None:
            return ResultCell(variable, ResultCellStatus.NON_FINITE, source_value, source_unit, None, "source value is non-finite")
        return ResultCell(variable, ResultCellStatus.AVAILABLE, source_value, source_unit, normalized, None)

    def _append_log(
        self,
        severity: LogSeverity,
        category: str,
        message: str,
        *,
        execution_id: str | None = None,
    ) -> None:
        self._logs.append(LogEntry(len(self._logs), execution_id, severity, category, message, self._now()))

    @staticmethod
    def _context_signature(context: ContextObservation) -> tuple[object, ...]:
        return (
            context.project_key,
            context.study_case_key,
            context.operational_scenario_key,
            context.variant_stages,
            context.active_grid_keys,
            context.configuration_key,
        )

    def _encode_cursor(self, kind: str, binding: str, offset: int) -> PageCursor:
        payload = canonical_json({"binding": binding, "kind": kind, "offset": offset}).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
        signature = hmac.new(_CURSOR_SECRET, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        return PageCursor(f"{encoded}.{signature}")

    def _decode_cursor(self, cursor: PageCursor | None, kind: str, binding: str) -> int:
        if cursor is None:
            return 0
        try:
            encoded, signature = cursor.token.split(".", 1)
            expected = hmac.new(_CURSOR_SECRET, encoded.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature mismatch")
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"binding", "kind", "offset"}:
                raise ValueError("invalid cursor payload")
            if payload["binding"] != binding or payload["kind"] != kind:
                raise ValueError("cursor binding mismatch")
            offset = payload["offset"]
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ValueError("invalid cursor offset")
            return offset
        except (binascii.Error, KeyError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise CursorInvalid("cursor is invalid for this primitive read") from exc


__all__ = ["DeterministicPrimitiveGateway"]
