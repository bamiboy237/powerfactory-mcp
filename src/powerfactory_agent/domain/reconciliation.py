"""Portable, immutable evidence contracts for crash reconciliation.

The records here describe evidence only.  They do not perform live reads,
classify an outcome, or admit a retry; those responsibilities belong to the
future reconciliation store and workflow orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from .gateway import AttributeSelector
from .models import VersionedName
from .values import (
    ConfigurationKey,
    ContentDigest,
    LiveStateFingerprint,
    ProductIdentity,
    Quantity,
    WorkspaceRevision,
    WorkflowVersion,
    require_aware,
    require_collection,
    require_text,
    require_uuid4,
)


class ObservationSource(str, Enum):
    OWNER_RETURN = "owner_return"
    LIVE_READ = "live_read"
    RECOVERY = "recovery"


class ObservationOutcome(str, Enum):
    VALUE_OBSERVED = "value_observed"
    PRECONDITION_REJECTED = "precondition_rejected"
    EFFECT_UNCERTAIN = "effect_uncertain"
    CLIENT_TIMED_OUT = "client_timed_out"
    OWNER_UNAVAILABLE = "owner_unavailable"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    CONTEXT_UNVERIFIED = "context_unverified"
    IDENTITY_UNVERIFIED = "identity_unverified"
    LOCATOR_UNVERIFIED = "locator_unverified"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    UNIT_UNVERIFIED = "unit_unverified"
    PERSISTENCE_UNAVAILABLE = "persistence_unavailable"


class ReconciliationClassification(str, Enum):
    BEFORE = "before"
    AFTER_OBSERVED = "after_observed"
    DIVERGED = "diverged"
    UNAVAILABLE = "unavailable"


class ManualDisposition(str, Enum):
    CONFIRM_BEFORE = "confirm_before"
    CONFIRM_AFTER_OBSERVED = "confirm_after_observed"
    ABANDON = "abandon"
    AUTHORIZE_COMPENSATION = "authorize_compensation"


def _require_positive(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")


def _require_uuid_tuple(value: tuple[str, ...], field_name: str, *, allow_empty: bool) -> None:
    require_collection(value, field_name, allow_empty=allow_empty)
    for item in value:
        require_uuid4(item, f"{field_name} entry")
    if len(set(value)) != len(value):
        raise ValueError(f"{field_name} cannot contain duplicates")


def _require_policy_versions(value: tuple[VersionedName, ...], field_name: str) -> None:
    require_collection(value, field_name, allow_empty=False)
    names = tuple(item.name for item in value)
    if len(set(names)) != len(names):
        raise ValueError(f"{field_name} cannot contain duplicate policy names")


@dataclass(frozen=True, slots=True)
class WriteAheadIntent:
    """One committed intent for exactly one target attribute before a write."""

    intent_id: str
    operation_id: str
    workflow_id: str
    workflow_version: WorkflowVersion
    idempotency_key: str
    execution_id: str
    lease_id: str
    fencing_token: int
    workspace_id: str
    workspace_revision: WorkspaceRevision
    product_identity: ProductIdentity
    locator_version_id: str
    attribute: AttributeSelector
    expected_before: Quantity
    proposed_after: Quantity
    configuration_key: ConfigurationKey
    live_state_fingerprint: LiveStateFingerprint
    request_digest: ContentDigest
    policy_versions: tuple[VersionedName, ...]
    owner_instance_id: str
    session_id: str
    correlation_id: str
    attempt_number: int
    created_at: datetime
    intent_digest: ContentDigest

    def __post_init__(self) -> None:
        for field_name in (
            "intent_id",
            "operation_id",
            "workflow_id",
            "lease_id",
            "workspace_id",
            "locator_version_id",
            "owner_instance_id",
            "correlation_id",
        ):
            require_uuid4(getattr(self, field_name), f"WriteAheadIntent.{field_name}")
        if self.workflow_version.scope_id != self.workflow_id:
            raise ValueError("workflow_version scope must equal workflow_id")
        if self.workspace_revision.scope_id != self.workspace_id:
            raise ValueError("workspace_revision scope must equal workspace_id")
        require_text(self.idempotency_key, "WriteAheadIntent.idempotency_key", maximum=256)
        require_uuid4(self.execution_id, "WriteAheadIntent.execution_id")
        _require_positive(self.fencing_token, "WriteAheadIntent.fencing_token")
        if not isinstance(self.attribute, AttributeSelector):
            raise TypeError("WriteAheadIntent.attribute must be an AttributeSelector")
        if self.expected_before.unit != self.proposed_after.unit:
            raise ValueError("expected_before and proposed_after must have the same unit")
        _require_policy_versions(self.policy_versions, "WriteAheadIntent.policy_versions")
        require_text(self.session_id, "WriteAheadIntent.session_id", maximum=256)
        _require_positive(self.attempt_number, "WriteAheadIntent.attempt_number")
        require_aware(self.created_at, "WriteAheadIntent.created_at")


@dataclass(frozen=True, slots=True)
class ReconciliationObservation:
    """A bounded, append-only fact observed after an intent was committed."""

    observation_id: str
    intent_id: str
    operation_id: str
    attempt_id: str
    source: ObservationSource
    outcome: ObservationOutcome
    observed_at: datetime
    diagnostic_reference: str
    observed_value: Optional[Quantity] = None
    configuration_key: Optional[ConfigurationKey] = None
    live_state_fingerprint: Optional[LiveStateFingerprint] = None
    workspace_revision: Optional[WorkspaceRevision] = None

    def __post_init__(self) -> None:
        for field_name in ("observation_id", "intent_id", "operation_id", "attempt_id"):
            require_uuid4(getattr(self, field_name), f"ReconciliationObservation.{field_name}")
        require_aware(self.observed_at, "ReconciliationObservation.observed_at")
        require_text(self.diagnostic_reference, "ReconciliationObservation.diagnostic_reference", maximum=1024)
        has_fresh_bindings = (
            self.configuration_key is not None
            and self.live_state_fingerprint is not None
            and self.workspace_revision is not None
        )
        if self.outcome is ObservationOutcome.VALUE_OBSERVED:
            if self.observed_value is None or not has_fresh_bindings:
                raise ValueError("value observations require value and fresh configuration, fingerprint, and workspace evidence")
        elif self.observed_value is not None:
            raise ValueError("non-value observations cannot claim an observed value")
        elif any(
            value is not None
            for value in (self.configuration_key, self.live_state_fingerprint, self.workspace_revision)
        ):
            raise ValueError("fresh observation bindings must be present together")


@dataclass(frozen=True, slots=True)
class ReconciliationRecord:
    """An append-only classification of an intent using durable observations."""

    reconciliation_id: str
    intent_id: str
    workflow_id: str
    workflow_version: WorkflowVersion
    classification: ReconciliationClassification
    observation_ids: tuple[str, ...]
    classified_at: datetime
    evidence_reference: str
    quarantine_reference: Optional[str] = None
    manual_disposition: Optional[ManualDisposition] = None
    operator_principal_reference: Optional[str] = None
    completed_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        for field_name in ("reconciliation_id", "intent_id", "workflow_id"):
            require_uuid4(getattr(self, field_name), f"ReconciliationRecord.{field_name}")
        if self.workflow_version.scope_id != self.workflow_id:
            raise ValueError("workflow_version scope must equal workflow_id")
        _require_uuid_tuple(self.observation_ids, "ReconciliationRecord.observation_ids", allow_empty=False)
        require_aware(self.classified_at, "ReconciliationRecord.classified_at")
        require_text(self.evidence_reference, "ReconciliationRecord.evidence_reference", maximum=1024)
        if self.quarantine_reference is not None:
            require_text(self.quarantine_reference, "ReconciliationRecord.quarantine_reference", maximum=1024)
        if self.classification in (
            ReconciliationClassification.DIVERGED,
            ReconciliationClassification.UNAVAILABLE,
        ) and self.quarantine_reference is None:
            raise ValueError("diverged and unavailable classifications require a quarantine reference")
        if self.manual_disposition is None:
            if self.operator_principal_reference is not None:
                raise ValueError("operator principal requires a manual disposition")
        elif self.operator_principal_reference is None:
            raise ValueError("manual dispositions require an independent operator principal reference")
        else:
            require_text(
                self.operator_principal_reference,
                "ReconciliationRecord.operator_principal_reference",
                maximum=256,
            )
        if self.completed_at is not None:
            require_aware(self.completed_at, "ReconciliationRecord.completed_at")
            if self.completed_at < self.classified_at:
                raise ValueError("completed_at cannot precede classified_at")


__all__ = [
    "ManualDisposition",
    "ObservationOutcome",
    "ObservationSource",
    "ReconciliationClassification",
    "ReconciliationObservation",
    "ReconciliationRecord",
    "WriteAheadIntent",
]
