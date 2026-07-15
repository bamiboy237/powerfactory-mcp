"""Immutable workflow, command, and audit contracts.

These records are deliberately persistence-agnostic.  They establish the
identity, compare-and-swap, idempotency, and audit bindings required before a
workflow service can admit a live effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from .models import VersionedName
from .values import (
    ConfigurationKey,
    ContentDigest,
    WorkflowVersion,
    require_aware,
    require_text,
    require_uuid4,
)


class WorkflowState(str, Enum):
    NEW = "new"
    PREVIEWING = "previewing"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    EXECUTION_ADMISSION = "execution_admission"
    EXECUTING = "executing"
    CALCULATING = "calculating"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED_BEFORE_EFFECT = "failed_before_effect"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    QUARANTINED = "quarantined"
    ROLLBACK_PREVIEWING = "rollback_previewing"
    AWAITING_ROLLBACK_AUTHORIZATION = "awaiting_rollback_authorization"
    ROLLBACK_ADMISSION = "rollback_admission"
    ROLLING_BACK = "rolling_back"
    ROLLBACK_VERIFYING = "rollback_verifying"
    ROLLED_BACK = "rolled_back"
    ABANDONED = "abandoned"


class WorkflowCommandStatus(str, Enum):
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    REJECTED = "rejected"
    VERSION_CONFLICT = "version_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    QUARANTINED = "quarantined"


class AuditEventType(str, Enum):
    REQUESTED = "requested"
    PREVIEW_STARTED = "preview_started"
    PREVIEW_PERSISTED = "preview_persisted"
    LEASE_RELEASED = "lease_released"
    AUTHORIZATION_REQUESTED = "authorization_requested"
    AUTHORIZATION_OBSERVED = "authorization_observed"
    ADMISSION_REVALIDATED = "admission_revalidated"
    INTENT_COMMITTED = "intent_committed"
    SUBMITTED = "submitted"
    OWNER_RETURNED = "owner_returned"
    CLIENT_TIMED_OUT = "client_timed_out"
    LIVE_OBSERVED = "live_observed"
    VERIFIED = "verified"
    RECOVERY_STARTED = "recovery_started"
    CLASSIFIED = "classified"
    QUARANTINED = "quarantined"
    MANUAL_DISPOSITION = "manual_disposition"
    RECONCILED = "reconciled"


class AuditActorClass(str, Enum):
    REQUESTER = "requester"
    WORKFLOW_SERVICE = "workflow_service"
    APPROVAL_AUTHORITY = "approval_authority"
    LEASE_MANAGER = "lease_manager"
    SERIALIZED_OWNER = "serialized_owner"
    RECOVERY_SERVICE = "recovery_service"
    OPERATOR = "operator"


def _require_optional_uuid4(value: Optional[str], field_name: str) -> None:
    if value is not None:
        require_uuid4(value, field_name)


def _require_optional_text(value: Optional[str], field_name: str, *, maximum: int) -> None:
    if value is not None:
        require_text(value, field_name, maximum=maximum)


@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    """The current durable state and immutable bindings for one workflow."""

    workflow_id: str
    state: WorkflowState
    workflow_version: WorkflowVersion
    operation_specification: VersionedName
    configuration_key: ConfigurationKey
    proposal_digest: ContentDigest
    created_at: datetime
    updated_at: datetime
    latest_command_id: Optional[str] = None
    latest_operation_id: Optional[str] = None
    recovery_reference: Optional[str] = None

    def __post_init__(self) -> None:
        require_uuid4(self.workflow_id, "WorkflowRecord.workflow_id")
        if self.workflow_version.scope_id != self.workflow_id:
            raise ValueError("workflow_version scope must equal workflow_id")
        require_aware(self.created_at, "WorkflowRecord.created_at")
        require_aware(self.updated_at, "WorkflowRecord.updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        _require_optional_uuid4(self.latest_command_id, "WorkflowRecord.latest_command_id")
        _require_optional_uuid4(self.latest_operation_id, "WorkflowRecord.latest_operation_id")
        _require_optional_text(
            self.recovery_reference,
            "WorkflowRecord.recovery_reference",
            maximum=1024,
        )


@dataclass(frozen=True, slots=True)
class IdempotentCommandRecord:
    """One durable command result, uniquely scoped by workflow and key."""

    command_id: str
    workflow_id: str
    command_name: str
    idempotency_key: str
    request_digest: ContentDigest
    expected_workflow_version: WorkflowVersion
    status: WorkflowCommandStatus
    resulting_workflow_version: WorkflowVersion
    requested_at: datetime
    operation_id: Optional[str] = None
    result_digest: Optional[ContentDigest] = None
    completed_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        require_uuid4(self.command_id, "IdempotentCommandRecord.command_id")
        require_uuid4(self.workflow_id, "IdempotentCommandRecord.workflow_id")
        require_text(self.command_name, "IdempotentCommandRecord.command_name", maximum=128)
        require_text(self.idempotency_key, "IdempotentCommandRecord.idempotency_key", maximum=256)
        require_aware(self.requested_at, "IdempotentCommandRecord.requested_at")
        _require_optional_uuid4(self.operation_id, "IdempotentCommandRecord.operation_id")
        if self.expected_workflow_version.scope_id != self.workflow_id:
            raise ValueError("expected_workflow_version scope must equal workflow_id")
        if self.resulting_workflow_version.scope_id != self.workflow_id:
            raise ValueError("resulting_workflow_version scope must equal workflow_id")
        if self.resulting_workflow_version.counter < self.expected_workflow_version.counter:
            raise ValueError("resulting_workflow_version cannot precede expected_workflow_version")
        if self.completed_at is not None:
            require_aware(self.completed_at, "IdempotentCommandRecord.completed_at")
            if self.completed_at < self.requested_at:
                raise ValueError("completed_at cannot precede requested_at")
        if self.status in (WorkflowCommandStatus.REQUESTED, WorkflowCommandStatus.IN_PROGRESS):
            if self.completed_at is not None or self.result_digest is not None:
                raise ValueError("incomplete command records cannot have a result")
        elif self.completed_at is None:
            raise ValueError("completed command records require completed_at")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """A sanitized append-only fact about a workflow command or transition."""

    event_id: str
    workflow_id: str
    workflow_version: WorkflowVersion
    event_type: AuditEventType
    actor_class: AuditActorClass
    occurred_at: datetime
    request_digest: ContentDigest
    evidence_reference: str
    command_id: Optional[str] = None
    operation_id: Optional[str] = None
    correlation_id: Optional[str] = None
    state_before: Optional[WorkflowState] = None
    state_after: Optional[WorkflowState] = None
    authorization_reference: Optional[str] = None
    fencing_token: Optional[int] = None

    def __post_init__(self) -> None:
        require_uuid4(self.event_id, "AuditEvent.event_id")
        require_uuid4(self.workflow_id, "AuditEvent.workflow_id")
        if self.workflow_version.scope_id != self.workflow_id:
            raise ValueError("workflow_version scope must equal workflow_id")
        require_aware(self.occurred_at, "AuditEvent.occurred_at")
        require_text(self.evidence_reference, "AuditEvent.evidence_reference", maximum=1024)
        _require_optional_uuid4(self.command_id, "AuditEvent.command_id")
        _require_optional_uuid4(self.operation_id, "AuditEvent.operation_id")
        _require_optional_uuid4(self.correlation_id, "AuditEvent.correlation_id")
        _require_optional_text(
            self.authorization_reference,
            "AuditEvent.authorization_reference",
            maximum=256,
        )
        if self.fencing_token is not None:
            if isinstance(self.fencing_token, bool) or not isinstance(self.fencing_token, int):
                raise TypeError("AuditEvent.fencing_token must be an integer")
            if self.fencing_token <= 0:
                raise ValueError("AuditEvent.fencing_token must be positive")
        if (self.state_before is None) != (self.state_after is None):
            raise ValueError("state_before and state_after must be present together")
