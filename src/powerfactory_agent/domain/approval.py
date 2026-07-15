"""Immutable approval-authority contracts for one durable workflow execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .models import ApprovalRequest, ExecutionAuthorization, VersionedName
from .values import (
    ConfigurationKey,
    ContentDigest,
    LiveStateFingerprint,
    MutationStrategy,
    OperationType,
    WorkspaceRevision,
    WorkflowVersion,
    require_aware,
    require_collection,
    require_text,
    require_uuid4,
)


class ApprovalDecisionKind(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    CANCELLED = "cancelled"


class AuthorizationState(str, Enum):
    ISSUED = "issued"
    CONSUMING = "consuming"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class AdmissionStatus(str, Enum):
    ADMITTED = "admitted"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


def _require_policy_versions(value: tuple[VersionedName, ...], field_name: str) -> None:
    require_collection(value, field_name, allow_empty=False)
    names = tuple((item.name, item.version) for item in value)
    if len(set(names)) != len(names):
        raise ValueError(f"{field_name} cannot contain duplicate policy versions")


@dataclass(frozen=True, slots=True)
class AuthorityApprovalRequest:
    """Authority-specific immutable bindings for an existing preview request."""

    approval_request: ApprovalRequest
    workflow_id: str
    configuration_key: ConfigurationKey
    live_state_fingerprint: LiveStateFingerprint
    workspace_revision: WorkspaceRevision
    operation_type: OperationType
    mutation_strategy: MutationStrategy
    expected_workflow_version: WorkflowVersion
    policy_versions: tuple[VersionedName, ...]
    summary_digest: ContentDigest

    def __post_init__(self) -> None:
        require_uuid4(self.approval_request.approval_request_id, "AuthorityApprovalRequest.approval_request_id")
        require_uuid4(self.approval_request.preview_id, "AuthorityApprovalRequest.preview_id")
        require_uuid4(self.workflow_id, "AuthorityApprovalRequest.workflow_id")
        if self.expected_workflow_version.scope_id != self.workflow_id:
            raise ValueError("expected_workflow_version scope must equal workflow_id")
        _require_policy_versions(self.policy_versions, "AuthorityApprovalRequest.policy_versions")


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    """A terminal, append-only authority outcome for one request."""

    decision_id: str
    approval_request_id: str
    authority_instance_id: str
    decision_kind: ApprovalDecisionKind
    decided_at: datetime
    authenticated_principal_reference: str | None
    reason_code: str

    def __post_init__(self) -> None:
        for field_name in ("decision_id", "approval_request_id", "authority_instance_id"):
            require_uuid4(getattr(self, field_name), f"AuthorityDecision.{field_name}")
        require_aware(self.decided_at, "AuthorityDecision.decided_at")
        require_text(self.reason_code, "AuthorityDecision.reason_code", maximum=128)
        if self.authenticated_principal_reference is not None:
            require_text(
                self.authenticated_principal_reference,
                "AuthorityDecision.authenticated_principal_reference",
                maximum=256,
            )
        if self.decision_kind is ApprovalDecisionKind.APPROVED and self.authenticated_principal_reference is None:
            raise ValueError("approved decisions require an authenticated principal reference")
        if self.decision_kind is not ApprovalDecisionKind.APPROVED and self.authenticated_principal_reference is not None:
            raise ValueError("only approved decisions may retain an authenticated principal reference")


@dataclass(frozen=True, slots=True)
class AuthorityAuthorization:
    """The non-delegable authority bindings missing from the legacy authorization."""

    execution_authorization: ExecutionAuthorization
    authority_decision_id: str
    authority_instance_id: str
    preview_id: str
    workspace_revision: WorkspaceRevision
    policy_versions: tuple[VersionedName, ...]

    def __post_init__(self) -> None:
        require_uuid4(self.authority_decision_id, "AuthorityAuthorization.authority_decision_id")
        require_uuid4(self.authority_instance_id, "AuthorityAuthorization.authority_instance_id")
        require_uuid4(self.preview_id, "AuthorityAuthorization.preview_id")
        _require_policy_versions(self.policy_versions, "AuthorityAuthorization.policy_versions")


@dataclass(frozen=True, slots=True)
class AuthorizationAdmission:
    """One durable admission result. Replays return this exact recorded fact."""

    execution_id: str
    workflow_id: str
    status: AdmissionStatus
    reason_code: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        require_uuid4(self.execution_id, "AuthorizationAdmission.execution_id")
        require_uuid4(self.workflow_id, "AuthorizationAdmission.workflow_id")
        require_text(self.reason_code, "AuthorizationAdmission.reason_code", maximum=128)
        require_aware(self.recorded_at, "AuthorizationAdmission.recorded_at")


__all__ = [
    "AdmissionStatus",
    "ApprovalDecisionKind",
    "AuthorityApprovalRequest",
    "AuthorityAuthorization",
    "AuthorityDecision",
    "AuthorizationAdmission",
    "AuthorizationState",
]
