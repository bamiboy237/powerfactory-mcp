"""Immutable durable envelope for a fully admitted execution write."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .values import ContentDigest, WorkflowVersion, require_aware, require_uuid4


def _require_positive(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")


@dataclass(frozen=True, slots=True)
class ExecutionAdmissionEnvelope:
    """The single durable handoff permitted before owner submission.

    Creating this envelope does not submit a PowerFactory call.  It only proves
    that authorization, workflow, lease, and write-ahead intent were committed
    together; an owner may submit the referenced operation only afterwards.
    """

    admission_id: str
    workflow_id: str
    workflow_version: WorkflowVersion
    command_id: str
    operation_id: str
    execution_id: str
    lease_id: str
    fencing_token: int
    intent_id: str
    request_digest: ContentDigest
    admitted_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "admission_id",
            "workflow_id",
            "command_id",
            "operation_id",
            "execution_id",
            "lease_id",
            "intent_id",
        ):
            require_uuid4(getattr(self, field_name), f"ExecutionAdmissionEnvelope.{field_name}")
        if self.workflow_version.scope_id != self.workflow_id:
            raise ValueError("workflow_version scope must equal workflow_id")
        _require_positive(self.fencing_token, "ExecutionAdmissionEnvelope.fencing_token")
        require_aware(self.admitted_at, "ExecutionAdmissionEnvelope.admitted_at")


__all__ = ["ExecutionAdmissionEnvelope"]
