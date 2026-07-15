"""Validated value types shared by the PowerFactory-independent domain model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from typing import ClassVar, Optional, Tuple
from uuid import UUID


MAX_TEXT_LENGTH = 4096
MAX_COLLECTION_LENGTH = 10_000
MAX_PAGE_SIZE = 100
_DIGEST_RE = re.compile(r"^(?P<kind>[a-z][a-z0-9-]*):(?P<schema>[a-z0-9][a-z0-9.-]*):sha256:(?P<hex>[0-9a-f]{64})$")


def require_text(value: str, field_name: str, *, maximum: int = MAX_TEXT_LENGTH) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be nonempty and have no surrounding whitespace")
    if len(value) > maximum:
        raise ValueError(f"{field_name} exceeds {maximum} characters")


def require_collection(value: tuple[object, ...], field_name: str, *, allow_empty: bool = True) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be an immutable tuple")
    if not allow_empty and not value:
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > MAX_COLLECTION_LENGTH:
        raise ValueError(f"{field_name} exceeds {MAX_COLLECTION_LENGTH} entries")


def require_aware(timestamp: datetime, field_name: str) -> None:
    if not isinstance(timestamp, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def require_uuid4(value: str, field_name: str) -> None:
    require_text(value, field_name, maximum=36)
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field_name} must be a canonical lowercase UUIDv4")


class FreshnessLevel(str, Enum):
    CACHED = "cached"
    VERIFIED = "verified"
    LIVE = "live"


class CompletenessState(str, Enum):
    COMPLETE = "complete"
    CONSERVATIVE = "conservative"
    UNSUPPORTED = "unsupported"


class AssetKind(str, Enum):
    AREA = "area"
    BUS = "bus"
    LINE = "line"
    LOAD = "load"
    TERMINAL = "terminal"
    TRANSFORMER = "transformer"


class LocatorKind(str, Enum):
    NATIVE_CANDIDATE = "native_candidate"
    CANONICAL_PATH_FALLBACK = "canonical_path_fallback"


class LocatorTrust(str, Enum):
    CANDIDATE = "candidate"
    FALLBACK = "fallback"
    VERIFIED_NATIVE = "verified_native"
    REJECTED = "rejected"


class IdentityLifecycleState(str, Enum):
    ACTIVE = "active"
    UNRESOLVED = "unresolved"
    TOMBSTONED = "tombstoned"


class OperationType(str, Enum):
    AREA_LOAD_SCALING = "area_load_scaling"
    ATTRIBUTE_CHANGE = "attribute_change"
    ROLLBACK = "rollback"


class MutationStrategy(str, Enum):
    DIRECT_LEDGER = "direct_ledger"
    SCENARIO_ISOLATION = "scenario_isolation"


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ConvergenceState(str, Enum):
    CONVERGED = "converged"
    NOT_CONVERGED = "not_converged"
    FAILED = "failed"


class ViolationSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ViolationTrend(str, Enum):
    NEW = "new"
    RESOLVED = "resolved"
    UNCHANGED = "unchanged"


class ConvergenceChange(str, Enum):
    UNCHANGED_CONVERGED = "unchanged_converged"
    UNCHANGED_NOT_CONVERGED = "unchanged_not_converged"
    BECAME_CONVERGED = "became_converged"
    BECAME_NOT_CONVERGED = "became_not_converged"


class WorkspaceDisposition(str, Enum):
    KEEP = "keep"
    REMOVE = "remove"
    RESTORE = "restore"


@dataclass(frozen=True, slots=True)
class ProductIdentity:
    value: str

    def __post_init__(self) -> None:
        require_uuid4(self.value, "ProductIdentity.value")


@dataclass(frozen=True, slots=True)
class ProjectProvenance:
    installation_id: str
    profile_id: str
    project_key: str
    project_evidence: str

    def __post_init__(self) -> None:
        require_text(self.installation_id, "ProjectProvenance.installation_id", maximum=256)
        require_text(self.profile_id, "ProjectProvenance.profile_id", maximum=256)
        require_text(self.project_key, "ProjectProvenance.project_key", maximum=512)
        require_text(self.project_evidence, "ProjectProvenance.project_evidence")


@dataclass(frozen=True, slots=True)
class LocatorEvidenceSchema:
    name: str
    version: str
    gateway_adapter_version: str

    def __post_init__(self) -> None:
        require_text(self.name, "LocatorEvidenceSchema.name", maximum=128)
        require_text(self.version, "LocatorEvidenceSchema.version", maximum=64)
        require_text(self.gateway_adapter_version, "LocatorEvidenceSchema.gateway_adapter_version", maximum=128)


@dataclass(frozen=True, slots=True)
class PowerFactoryLocator:
    """Immutable versioned locator evidence, never product identity."""

    locator_version_id: str
    locator_kind: LocatorKind
    project_provenance: ProjectProvenance
    object_class: str
    native_field: Optional[str]
    native_value: Optional[str]
    canonical_path: Optional[str]
    evidence_schema: LocatorEvidenceSchema
    observed_at: datetime
    session_id: str
    trust: LocatorTrust
    native_evidence_accepted: bool = False

    def __post_init__(self) -> None:
        require_uuid4(self.locator_version_id, "PowerFactoryLocator.locator_version_id")
        require_text(self.object_class, "PowerFactoryLocator.object_class", maximum=128)
        require_aware(self.observed_at, "PowerFactoryLocator.observed_at")
        require_text(self.session_id, "PowerFactoryLocator.session_id", maximum=256)
        if (self.native_field is None) != (self.native_value is None):
            raise ValueError("native_field and native_value must both be present or absent")
        if self.native_field is not None:
            require_text(self.native_field, "PowerFactoryLocator.native_field", maximum=128)
            require_text(self.native_value, "PowerFactoryLocator.native_value")  # type: ignore[arg-type]
        if self.canonical_path is not None:
            require_text(self.canonical_path, "PowerFactoryLocator.canonical_path")
        if self.locator_kind is LocatorKind.NATIVE_CANDIDATE and self.native_value is None:
            raise ValueError("native_candidate locators require native evidence")
        if self.locator_kind is LocatorKind.CANONICAL_PATH_FALLBACK and self.canonical_path is None:
            raise ValueError("canonical_path_fallback locators require a canonical path")
        if self.trust is LocatorTrust.VERIFIED_NATIVE and not self.native_evidence_accepted:
            raise ValueError("verified_native requires explicit accepted Windows evidence")


@dataclass(frozen=True, slots=True)
class ConfigurationKey:
    value: str

    DIGEST_KIND: ClassVar[str] = "configuration-key"
    DIGEST_SCHEMA: ClassVar[str] = "v1"

    def __post_init__(self) -> None:
        _require_typed_digest(self.value, self.DIGEST_KIND, self.DIGEST_SCHEMA, type(self).__name__)


def _require_typed_digest(value: str, kind: str, schema: str, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name}.value must be a string")
    match = _DIGEST_RE.fullmatch(value)
    if match is None or match.group("kind") != kind or match.group("schema") != schema:
        raise ValueError(f"{field_name}.value must be {kind}:{schema}:sha256:<lowercase-hex>")


@dataclass(frozen=True, slots=True)
class _DigestValue:
    value: str

    DIGEST_KIND: ClassVar[str] = ""
    DIGEST_SCHEMA: ClassVar[str] = "v1"

    def __post_init__(self) -> None:
        _require_typed_digest(self.value, self.DIGEST_KIND, self.DIGEST_SCHEMA, type(self).__name__)


@dataclass(frozen=True, slots=True)
class LiveStateFingerprint(_DigestValue):
    DIGEST_KIND: ClassVar[str] = "live-state-fingerprint"


@dataclass(frozen=True, slots=True)
class CalculationInputDigest(_DigestValue):
    DIGEST_KIND: ClassVar[str] = "calculation-input"


@dataclass(frozen=True, slots=True)
class ContentDigest(_DigestValue):
    DIGEST_KIND: ClassVar[str] = "content"


@dataclass(frozen=True, slots=True)
class _Revision:
    scope_id: str
    counter: int

    WIRE_KIND: ClassVar[str] = ""

    def __post_init__(self) -> None:
        require_uuid4(self.scope_id, f"{type(self).__name__}.scope_id")
        if isinstance(self.counter, bool) or not isinstance(self.counter, int) or self.counter < 0:
            raise ValueError(f"{type(self).__name__}.counter must be a nonnegative integer")

    @property
    def wire(self) -> str:
        return f"{self.WIRE_KIND}/v1:{self.scope_id}:{self.counter}"

    @classmethod
    def from_wire(cls, wire: str) -> "_Revision":
        if not isinstance(wire, str):
            raise TypeError("revision wire value must be a string")
        prefix = f"{cls.WIRE_KIND}/v1:"
        if not wire.startswith(prefix):
            raise ValueError(f"revision wire value must start with {prefix}")
        remainder = wire[len(prefix):]
        try:
            scope_id, counter_text = remainder.rsplit(":", 1)
            counter = int(counter_text)
        except (ValueError, TypeError) as exc:
            raise ValueError("revision wire value is malformed") from exc
        return cls(scope_id, counter)


@dataclass(frozen=True, slots=True)
class ExtractionRevision(_Revision):
    WIRE_KIND: ClassVar[str] = "extraction-revision"


@dataclass(frozen=True, slots=True)
class WorkspaceRevision(_Revision):
    WIRE_KIND: ClassVar[str] = "workspace-revision"


@dataclass(frozen=True, slots=True)
class WorkflowVersion(_Revision):
    WIRE_KIND: ClassVar[str] = "workflow-version"


@dataclass(frozen=True, slots=True)
class DependencySetIdentity:
    name: str
    version: str

    def __post_init__(self) -> None:
        require_text(self.name, "DependencySetIdentity.name", maximum=128)
        require_text(self.version, "DependencySetIdentity.version", maximum=64)


@dataclass(frozen=True, slots=True)
class FreshnessEvidence:
    level: FreshnessLevel
    observed_at: datetime
    session_id: str
    configuration_key: ConfigurationKey
    dependency_set: DependencySetIdentity
    evidence_reference: str
    policy_name: str
    policy_version: str
    operation_active: bool

    def __post_init__(self) -> None:
        require_aware(self.observed_at, "FreshnessEvidence.observed_at")
        require_text(self.session_id, "FreshnessEvidence.session_id", maximum=256)
        require_text(self.evidence_reference, "FreshnessEvidence.evidence_reference")
        require_text(self.policy_name, "FreshnessEvidence.policy_name", maximum=128)
        require_text(self.policy_version, "FreshnessEvidence.policy_version", maximum=64)
        if self.level is FreshnessLevel.LIVE and not self.operation_active:
            raise ValueError("LIVE freshness is valid only during an active serialized operation")


@dataclass(frozen=True, slots=True)
class Quantity:
    """A finite Decimal engineering value paired with an explicit unit."""

    value: Decimal
    unit: str

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or isinstance(self.value, float):
            raise TypeError("Quantity.value must be Decimal, integer, or canonical decimal string; float is forbidden")
        try:
            numeric = self.value if isinstance(self.value, Decimal) else Decimal(self.value)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Quantity.value is not a valid decimal") from exc
        if not numeric.is_finite():
            raise ValueError("Quantity.value must be finite")
        if numeric == 0:
            numeric = Decimal(0)
        object.__setattr__(self, "value", numeric)
        require_text(self.unit, "Quantity.unit", maximum=64)
