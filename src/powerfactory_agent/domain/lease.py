"""Immutable context-lease and fencing contracts.

These persistence-agnostic records bind one workflow to the exclusive live
PowerFactory context envelope.  A future transactional lease manager is
responsible for compare-and-swap and strict per-scope token monotonicity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from .values import (
    ConfigurationKey,
    ContentDigest,
    WorkflowVersion,
    require_aware,
    require_text,
    require_uuid4,
)


class LeaseMode(str, Enum):
    PREVIEW = "preview"
    EXECUTION = "execution"
    ROLLBACK = "rollback"


class LeaseState(str, Enum):
    AVAILABLE = "available"
    HELD_PREVIEW = "held_preview"
    HELD_EXECUTION = "held_execution"
    HELD_ROLLBACK = "held_rollback"
    EXPIRED = "expired"
    IN_FLIGHT_EXPIRED = "in_flight_expired"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    QUARANTINED = "quarantined"


class LeaseEventType(str, Enum):
    ACQUIRE_ATTEMPTED = "acquire_attempted"
    ACQUIRED = "acquired"
    RELEASED_FOR_AUTHORIZATION = "released_for_authorization"
    RELEASED = "released"
    EXPIRED = "expired"
    STALE_FENCE_REJECTED = "stale_fence_rejected"
    ATOMIC_CALL_STARTED = "atomic_call_started"
    ATOMIC_CALL_FINISHED = "atomic_call_finished"
    RECOVERY_ADMITTED = "recovery_admitted"
    QUARANTINED = "quarantined"
    OPERATOR_RECOVERED = "operator_recovered"


def _require_positive_token(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")


def _require_optional_uuid4(value: Optional[str], field_name: str) -> None:
    if value is not None:
        require_uuid4(value, field_name)


def _require_optional_text(value: Optional[str], field_name: str, *, maximum: int) -> None:
    if value is not None:
        require_text(value, field_name, maximum=maximum)


_HELD_STATE_FOR_MODE = {
    LeaseMode.PREVIEW: LeaseState.HELD_PREVIEW,
    LeaseMode.EXECUTION: LeaseState.HELD_EXECUTION,
    LeaseMode.ROLLBACK: LeaseState.HELD_ROLLBACK,
}
_HELD_STATES = frozenset(_HELD_STATE_FOR_MODE.values())
_RECOVERY_STATES = frozenset(
    {
        LeaseState.EXPIRED,
        LeaseState.IN_FLIGHT_EXPIRED,
        LeaseState.RECONCILIATION_REQUIRED,
        LeaseState.QUARANTINED,
    }
)


@dataclass(frozen=True, slots=True)
class ContextLease:
    """One admitted lease incarnation; ``AVAILABLE`` is represented by no row.

    ``service_scope_digest`` and ``configuration_key`` together form the lease
    scope.  The digest is a sanitized canonical singleton-service identity.
    """

    lease_id: str
    service_scope_digest: ContentDigest
    configuration_key: ConfigurationKey
    workflow_id: str
    workflow_version: WorkflowVersion
    fencing_token: int
    mode: LeaseMode
    state: LeaseState
    issued_at: datetime
    expires_at: datetime
    owner_instance_id: str
    operation_id: Optional[str] = None
    recovery_disposition: Optional[str] = None

    def __post_init__(self) -> None:
        require_uuid4(self.lease_id, "ContextLease.lease_id")
        require_uuid4(self.workflow_id, "ContextLease.workflow_id")
        if self.workflow_version.scope_id != self.workflow_id:
            raise ValueError("workflow_version scope must equal workflow_id")
        _require_positive_token(self.fencing_token, "ContextLease.fencing_token")
        require_aware(self.issued_at, "ContextLease.issued_at")
        require_aware(self.expires_at, "ContextLease.expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must follow issued_at")
        require_uuid4(self.owner_instance_id, "ContextLease.owner_instance_id")
        _require_optional_uuid4(self.operation_id, "ContextLease.operation_id")
        _require_optional_text(
            self.recovery_disposition,
            "ContextLease.recovery_disposition",
            maximum=1024,
        )
        if self.state is LeaseState.AVAILABLE:
            raise ValueError("AVAILABLE is represented by the absence of a ContextLease")
        if self.state in _HELD_STATES and self.state is not _HELD_STATE_FOR_MODE[self.mode]:
            raise ValueError("held lease state must match lease mode")
        if self.state is LeaseState.EXPIRED and self.operation_id is not None:
            raise ValueError("EXPIRED lease cannot retain an operation_id")
        if self.state is LeaseState.IN_FLIGHT_EXPIRED and self.operation_id is None:
            raise ValueError("IN_FLIGHT_EXPIRED lease requires an operation_id")
        if self.state in _HELD_STATES and self.recovery_disposition is not None:
            raise ValueError("held lease cannot have a recovery_disposition")
        if self.state in _RECOVERY_STATES and self.recovery_disposition is None:
            raise ValueError("recovery lease state requires a recovery_disposition")


@dataclass(frozen=True, slots=True)
class LeaseEvent:
    """A sanitized append-only fact about a context-lease transition."""

    event_id: str
    lease_id: str
    service_scope_digest: ContentDigest
    configuration_key: ConfigurationKey
    workflow_id: str
    workflow_version: WorkflowVersion
    fencing_token: int
    event_type: LeaseEventType
    occurred_at: datetime
    state_before: LeaseState
    state_after: LeaseState
    reason: str
    evidence_reference: str
    command_id: Optional[str] = None
    operation_id: Optional[str] = None
    correlation_id: Optional[str] = None

    def __post_init__(self) -> None:
        require_uuid4(self.event_id, "LeaseEvent.event_id")
        require_uuid4(self.lease_id, "LeaseEvent.lease_id")
        require_uuid4(self.workflow_id, "LeaseEvent.workflow_id")
        if self.workflow_version.scope_id != self.workflow_id:
            raise ValueError("workflow_version scope must equal workflow_id")
        _require_positive_token(self.fencing_token, "LeaseEvent.fencing_token")
        require_aware(self.occurred_at, "LeaseEvent.occurred_at")
        require_text(self.reason, "LeaseEvent.reason", maximum=1024)
        require_text(self.evidence_reference, "LeaseEvent.evidence_reference", maximum=1024)
        _require_optional_uuid4(self.command_id, "LeaseEvent.command_id")
        _require_optional_uuid4(self.operation_id, "LeaseEvent.operation_id")
        _require_optional_uuid4(self.correlation_id, "LeaseEvent.correlation_id")
        if self.event_type is LeaseEventType.ATOMIC_CALL_STARTED and self.operation_id is None:
            raise ValueError("ATOMIC_CALL_STARTED event requires an operation_id")
        if self.event_type is LeaseEventType.ATOMIC_CALL_FINISHED and self.operation_id is None:
            raise ValueError("ATOMIC_CALL_FINISHED event requires an operation_id")
