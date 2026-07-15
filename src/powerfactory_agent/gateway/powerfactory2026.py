"""Unvalidated PowerFactory 2026 implementation of the primitive gateway protocol.

The vendor seam intentionally accepts and returns only owned, typed values. A
Windows implementation may contain PowerFactory imports and raw DataObjects
behind that seam; this module never exposes either outside the gateway package.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
from typing import Callable, Protocol, Sequence, runtime_checkable

from powerfactory_agent.domain import (
    AttributeWriteObservation,
    AttributeWriteRequest,
    CleanupObservation,
    CommandCompletion,
    CommandExecutionObservation,
    CommandExecutionRequest,
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
    ObjectDependencyObservation,
    ObjectObservation,
    ObjectQueryBatch,
    ObjectQueryRequest,
    OutOfServicePolicy,
    PageCursor,
    PrimitiveField,
    PrimitiveObjectSelector,
    RelationshipObservation,
    ResultBatch,
    ResultCell,
    ResultCollectionRequest,
    ResultRow,
    SessionObservation,
    SessionStartRequest,
    VariantStageObservation,
)
from powerfactory_agent.gateway.errors import (
    ConfigurationMismatch,
    ConnectionFailure,
    CursorInvalid,
    GatewayError,
    InvalidOperation,
    ObjectNotFound,
)
from powerfactory_agent.serialization import canonical_digest


_ADAPTER_VERSION = "powerfactory2026/0.1.0-unvalidated"
_MAX_VENDOR_RECORDS = 10_000
_LOG_SECRET = re.compile(r"(?i)\b(password|secret|token|credential)\s*[:=]\s*\S+")


@dataclass(frozen=True, slots=True)
class VendorSession:
    """Scalar startup evidence returned by the Windows-only vendor seam."""

    session_id: str
    powerfactory_version: str
    python_abi: str
    architecture: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VendorContext:
    project_key: str | None
    study_case_key: str | None
    operational_scenario_key: str | None
    variant_stages: tuple[VariantStageObservation, ...]
    active_grid_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VendorObjectRecord:
    selector: PrimitiveObjectSelector
    display_name: str
    fields: tuple[PrimitiveField, ...]
    out_of_service: bool


@dataclass(frozen=True, slots=True)
class VendorDependencyRecord:
    selector: PrimitiveObjectSelector
    fields: tuple[PrimitiveField, ...]
    relationships: tuple[RelationshipObservation, ...]


@dataclass(frozen=True, slots=True)
class VendorCommandOutcome:
    execution_id: str
    return_code: int
    diagnostic_messages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VendorResultRecord:
    selector: PrimitiveObjectSelector
    cells: tuple[ResultCell, ...]


@dataclass(frozen=True, slots=True)
class VendorLogRecord:
    execution_id: str | None
    severity: LogSeverity
    category: str
    message: str


@runtime_checkable
class PowerFactory2026Vendor(Protocol):
    """Typed seam implemented inside the Windows PowerFactory boundary.

    ``start`` is the sole acquisition point. A Windows seam may compose the
    Buildout 0 lifecycle adapter internally only when both use the same vendor
    application handle; the gateway intentionally never runs lifecycle stages.
    """

    def start(self, request: SessionStartRequest) -> VendorSession: ...

    def inspect_context(self) -> VendorContext: ...

    def activate_context(self, request: ContextActivationRequest) -> VendorContext: ...

    def query_objects(self, request: ObjectQueryRequest) -> Sequence[VendorObjectRecord]: ...

    def observe_dependencies(
        self, request: DependencyReadRequest
    ) -> tuple[Sequence[VendorDependencyRecord], bool]: ...

    def execute_command(self, request: CommandExecutionRequest) -> VendorCommandOutcome: ...

    def collect_results(self, request: ResultCollectionRequest) -> Sequence[VendorResultRecord]: ...

    def read_logs(self) -> Sequence[VendorLogRecord]: ...

    def close(self) -> Sequence[str]: ...


class PowerFactoryGateway2026:
    """Bounded, read-only Buildout 2 gateway backed by an injected vendor seam."""

    def __init__(
        self,
        vendor: PowerFactory2026Vendor,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(vendor, PowerFactory2026Vendor):
            raise TypeError("vendor must implement PowerFactory2026Vendor")
        self._vendor = vendor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._start_request: SessionStartRequest | None = None
        self._session: SessionObservation | None = None
        self._context: ContextObservation | None = None
        self._executions: dict[str, tuple[CommandExecutionRequest, CommandExecutionObservation]] = {}
        self._logs: list[LogEntry] = []
        self._cursors: dict[str, tuple[str, str, int]] = {}
        self._cursor_sequence = 0

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
        try:
            vendor_session = self._vendor.start(request)
        except GatewayError:
            raise
        except Exception:
            raise ConnectionFailure("PowerFactory 2026 vendor session could not start") from None
        self._start_request = request
        self._session = SessionObservation(
            vendor_session.session_id,
            _ADAPTER_VERSION,
            vendor_session.powerfactory_version,
            vendor_session.python_abi,
            vendor_session.architecture,
            GatewayReadiness.READY,
            tuple(sorted(set(vendor_session.capabilities))),
            self._now(),
        )
        self._append_log(LogSeverity.INFO, "lifecycle", "PowerFactory 2026 gateway started")
        return self._session

    def inspect_context(self) -> ContextObservation:
        session = self._require_started()
        try:
            vendor_context = self._vendor.inspect_context()
        except GatewayError:
            raise
        except Exception:
            raise ConnectionFailure("PowerFactory context inspection failed") from None
        context = self._context_from_vendor(session.session_id, vendor_context)
        self._context = context
        return context

    def activate_context(
        self, request: ContextActivationRequest
    ) -> ContextActivationObservation:
        session = self._require_started()
        prior = self._context
        try:
            vendor_context = self._vendor.activate_context(request)
        except GatewayError:
            raise
        except Exception:
            raise ConnectionFailure("PowerFactory context activation failed") from None
        context = self._context_from_vendor(session.session_id, vendor_context)
        if not context.verified or (
            context.project_key != request.project_key
            or context.study_case_key != request.study_case_key
            or context.operational_scenario_key != request.operational_scenario_key
        ):
            raise ConfigurationMismatch("PowerFactory activation could not be verified")
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
        try:
            records = tuple(self._vendor.query_objects(request))
        except GatewayError:
            raise
        except Exception:
            raise ConnectionFailure("PowerFactory object query failed") from None
        self._assert_vendor_bound(records, "object query")
        admitted = set(request.object_classes)
        filtered = tuple(
            record
            for record in records
            if record.selector.object_class in admitted
            and self._out_of_service_matches(record.out_of_service, request.out_of_service)
        )
        page = filtered[offset : offset + request.limit]
        next_offset = offset + len(page)
        truncated = next_offset < len(filtered)
        warnings: list[GatewayWarning] = []
        unsupported = len(request.object_classes) - len(
            {record.selector.object_class for record in filtered}
        )
        if unsupported:
            warnings.append(
                GatewayWarning(
                    GatewayWarningCode.UNSUPPORTED_SELECTOR,
                    "one or more object-class selectors produced no vendor records",
                    unsupported,
                )
            )
        if truncated:
            warnings.append(
                GatewayWarning(
                    GatewayWarningCode.QUERY_TRUNCATED,
                    "object query was truncated at the requested entry limit",
                    len(filtered) - next_offset,
                )
            )
        return ObjectQueryBatch(
            request.configuration_key,
            tuple(ObjectObservation(item.selector, item.display_name, item.fields) for item in page),
            self._encode_cursor("objects", binding, next_offset) if truncated else None,
            not truncated,
            truncated,
            tuple(warnings),
            self._now(),
        )

    def observe_dependencies(self, request: DependencyReadRequest) -> DependencyObservation:
        self._require_configuration(request.configuration_key)
        if len(request.objects) > request.limit:
            raise InvalidOperation("dependency request exceeds its declared result limit")
        try:
            records, complete = self._vendor.observe_dependencies(request)
        except GatewayError:
            raise
        except Exception:
            raise ConnectionFailure("PowerFactory dependency observation failed") from None
        records = tuple(records)
        self._assert_vendor_bound(records, "dependency observation")
        if len(records) > request.limit:
            raise InvalidOperation("dependency observation exceeds its declared result limit")
        observations = tuple(
            ObjectDependencyObservation(item.selector, item.fields, item.relationships)
            for item in records
        )
        fingerprint = LiveStateFingerprint(
            canonical_digest(
                {
                    "configuration_key": request.configuration_key,
                    "objects": observations,
                    "complete": bool(complete),
                },
                kind="live-state-fingerprint",
            )
        )
        return DependencyObservation(
            request.configuration_key,
            observations,
            bool(complete),
            fingerprint,
            self._now(),
        )

    def execute_command(self, request: CommandExecutionRequest) -> CommandExecutionObservation:
        self._require_configuration(request.configuration_key)
        for original, observation in self._executions.values():
            if original.idempotency_key == request.idempotency_key:
                if original != request:
                    raise InvalidOperation("idempotency key is already bound to a different command")
                return observation
        started_at = self._now()
        try:
            outcome = self._vendor.execute_command(request)
        except GatewayError:
            raise
        except Exception:
            raise ConnectionFailure("PowerFactory command execution failed") from None
        if isinstance(outcome.return_code, bool) or not isinstance(outcome.return_code, int):
            raise InvalidOperation("vendor command return code must be an integer")
        observation = CommandExecutionObservation(
            outcome.execution_id,
            request.command,
            CommandCompletion.SUCCEEDED if outcome.return_code == 0 else CommandCompletion.FAILED,
            outcome.return_code,
            tuple(_safe_log_message(item) for item in outcome.diagnostic_messages),
            started_at,
            self._now(),
        )
        self._executions[observation.execution_id] = (request, observation)
        self._append_log(
            LogSeverity.INFO if outcome.return_code == 0 else LogSeverity.ERROR,
            "command",
            "command completed" if outcome.return_code == 0 else "command failed",
            execution_id=observation.execution_id,
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
        try:
            records = tuple(self._vendor.collect_results(request))
        except GatewayError:
            raise
        except Exception:
            raise ConnectionFailure("PowerFactory result collection failed") from None
        self._assert_vendor_bound(records, "result collection")
        requested = set(request.objects)
        rows = tuple(
            ResultRow(record.selector, record.cells)
            for record in records
            if record.selector in requested
        )
        page = rows[offset : offset + request.limit]
        next_offset = offset + len(page)
        truncated = next_offset < len(rows)
        return ResultBatch(
            request.execution_id,
            page,
            self._encode_cursor("results", binding, next_offset) if truncated else None,
            not truncated,
            self._now(),
        )

    def read_logs(self, request: LogReadRequest) -> LogBatch:
        self._require_started()
        binding = canonical_digest({"execution_id": request.execution_id}, kind="cursor-binding")
        offset = self._decode_cursor(request.cursor, "logs", binding)
        try:
            vendor_logs = tuple(self._vendor.read_logs())
        except GatewayError:
            raise
        except Exception:
            raise ConnectionFailure("PowerFactory log read failed") from None
        self._assert_vendor_bound(vendor_logs, "log read")
        entries: list[tuple[LogEntry, bool]] = [(entry, False) for entry in self._logs]
        for index, item in enumerate(vendor_logs):
            message = _safe_log_message(item.message)
            entries.append(
                (
                    LogEntry(
                    len(self._logs) + index,
                    item.execution_id,
                    item.severity,
                    item.category,
                    message,
                    self._now(),
                    ),
                    message != item.message,
                )
            )
        if request.execution_id is not None:
            entries = [item for item in entries if item[0].execution_id == request.execution_id]
        selected: list[tuple[LogEntry, bool]] = []
        byte_count = 0
        for entry, redacted in entries[offset : offset + request.entry_limit]:
            entry_bytes = len(entry.message.encode("utf-8"))
            if byte_count + entry_bytes > request.byte_limit:
                break
            selected.append((entry, redacted))
            byte_count += entry_bytes
        consumed = offset + len(selected)
        truncated = consumed < len(entries)
        return LogBatch(
            tuple(entry for entry, _ in selected),
            self._encode_cursor("logs", binding, consumed) if truncated else None,
            byte_count,
            truncated,
            any(redacted for _, redacted in selected),
        )

    def write_attribute(self, request: AttributeWriteRequest) -> AttributeWriteObservation:
        del request
        raise InvalidOperation("live PowerFactory writes are disabled in this gateway slice")

    def close(self) -> CleanupObservation:
        session = self._session
        if session is None:
            return CleanupObservation(None, False, True, (), self._now())
        diagnostics: list[str] = []
        succeeded = True
        try:
            diagnostics.extend(_safe_log_message(item) for item in self._vendor.close())
        except Exception:
            succeeded = False
            diagnostics.append("PowerFactory vendor cleanup failed")
        self._session = None
        self._start_request = None
        self._context = None
        self._executions.clear()
        self._logs.clear()
        self._cursors.clear()
        return CleanupObservation(session.session_id, True, succeeded, tuple(diagnostics), self._now())

    def _context_from_vendor(self, session_id: str, value: VendorContext) -> ContextObservation:
        verified = (
            value.project_key is not None
            and value.study_case_key is not None
            and bool(value.active_grid_keys)
        )
        configuration_key = (
            ConfigurationKey(
                canonical_digest(
                    {
                        "adapter": _ADAPTER_VERSION,
                        "project": value.project_key,
                        "study_case": value.study_case_key,
                        "scenario": value.operational_scenario_key,
                        "variant_stages": value.variant_stages,
                        "active_grid_keys": value.active_grid_keys,
                    },
                    kind="configuration-key",
                )
            )
            if verified
            else None
        )
        return ContextObservation(
            session_id,
            value.project_key,
            value.study_case_key,
            value.operational_scenario_key,
            value.variant_stages,
            value.active_grid_keys,
            configuration_key,
            verified,
            self._now(),
        )

    def _require_started(self) -> SessionObservation:
        if self._session is None:
            raise InvalidOperation("gateway has not been started")
        return self._session

    def _require_configuration(self, configuration_key: ConfigurationKey) -> ContextObservation:
        self._require_started()
        if self._context is None or not self._context.verified:
            raise InvalidOperation("gateway context has not been activated and verified")
        if configuration_key != self._context.configuration_key:
            raise ConfigurationMismatch("configuration key does not match the active context")
        return self._context

    def _append_log(
        self,
        severity: LogSeverity,
        category: str,
        message: str,
        *,
        execution_id: str | None = None,
    ) -> None:
        self._logs.append(
            LogEntry(len(self._logs), execution_id, severity, category, _safe_log_message(message), self._now())
        )

    def _encode_cursor(self, kind: str, binding: str, offset: int) -> PageCursor:
        self._cursor_sequence += 1
        token = f"{kind}-{self._cursor_sequence}.{hashlib.sha256(binding.encode()).hexdigest()[:16]}"
        self._cursors[token] = (kind, binding, offset)
        return PageCursor(token)

    def _decode_cursor(self, cursor: PageCursor | None, kind: str, binding: str) -> int:
        if cursor is None:
            return 0
        state = self._cursors.get(cursor.token)
        if state is None or state[:2] != (kind, binding):
            raise CursorInvalid("cursor is invalid for this gateway read")
        return state[2]

    @staticmethod
    def _assert_vendor_bound(values: Sequence[object], operation: str) -> None:
        if len(values) > _MAX_VENDOR_RECORDS:
            raise InvalidOperation(f"{operation} exceeded the vendor record ceiling")

    @staticmethod
    def _out_of_service_matches(value: bool, policy: OutOfServicePolicy) -> bool:
        return (
            policy is OutOfServicePolicy.INCLUDE
            or (policy is OutOfServicePolicy.EXCLUDE and not value)
            or (policy is OutOfServicePolicy.ONLY and value)
        )

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

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("clock must return an aware datetime")
        return value


def _safe_log_message(value: str) -> str:
    redacted = _LOG_SECRET.sub(lambda match: f"{match.group(1)}=[redacted]", value)
    normalized = " ".join(redacted.split())
    return normalized[:4096] or "PowerFactory vendor message"


__all__ = [
    "PowerFactory2026Vendor",
    "PowerFactoryGateway2026",
    "VendorCommandOutcome",
    "VendorContext",
    "VendorDependencyRecord",
    "VendorLogRecord",
    "VendorObjectRecord",
    "VendorResultRecord",
    "VendorSession",
]
