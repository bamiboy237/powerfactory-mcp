"""SQLite-backed local authority records and replay-safe execution admission."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
import uuid

from powerfactory_agent.domain.approval import (
    AdmissionStatus,
    ApprovalDecisionKind,
    AuthorityApprovalRequest,
    AuthorityAuthorization,
    AuthorityDecision,
    AuthorizationAdmission,
    AuthorizationState,
)
from powerfactory_agent.domain.models import ExecutionAuthorization
from powerfactory_agent.domain.workflow import WorkflowRecord, WorkflowState
from powerfactory_agent.domain.values import require_aware, require_uuid4
from powerfactory_agent.serialization import canonical_json, from_json

from .database import SQLiteDatabase


class AuthorityApprovalRequestNotFoundError(LookupError):
    pass


class AuthorityAuthorizationNotFoundError(LookupError):
    pass


class AuthorityTerminalDecisionError(RuntimeError):
    """A request has a terminal authority decision and cannot be decided again."""


class AuthorityRequestExpiredError(RuntimeError):
    pass


class ApprovalAuthorityStore:
    """Authority-only durable records; it cannot execute or mutate a model.

    The caller supplies a principal *reference* only after an external local
    authority has authenticated it.  This portable class deliberately does not
    provide an authentication route, token, session, or principal provider.
    """

    def __init__(self, database: SQLiteDatabase, *, authority_instance_id: str) -> None:
        require_uuid4(authority_instance_id, "authority_instance_id")
        self.database = database
        self.authority_instance_id = authority_instance_id

    def create_request(self, request: AuthorityApprovalRequest) -> AuthorityApprovalRequest:
        """Append one request only while the exact workflow awaits authority."""
        with self.database.transaction(immediate=True) as connection:
            workflow = self._workflow(connection, request.workflow_id)
            self._require_request_matches_workflow(request, workflow)
            try:
                connection.execute(
                    """INSERT INTO authority_approval_requests(
                    approval_request_id, workflow_id, expires_at, request_json
                    ) VALUES (?, ?, ?, ?)""",
                    (
                        request.approval_request.approval_request_id,
                        request.workflow_id,
                        _timestamp(request.approval_request.expires_at),
                        canonical_json(request),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AuthorityTerminalDecisionError("approval request ID already exists") from exc
        return request

    def request(self, approval_request_id: str) -> AuthorityApprovalRequest:
        require_uuid4(approval_request_id, "approval_request_id")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT request_json FROM authority_approval_requests WHERE approval_request_id = ?",
                (approval_request_id,),
            ).fetchone()
        if row is None:
            raise AuthorityApprovalRequestNotFoundError(approval_request_id)
        return from_json(AuthorityApprovalRequest, row["request_json"])

    def decision(self, approval_request_id: str) -> AuthorityDecision | None:
        require_uuid4(approval_request_id, "approval_request_id")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT decision_json FROM authority_decisions WHERE approval_request_id = ?",
                (approval_request_id,),
            ).fetchone()
        return None if row is None else from_json(AuthorityDecision, row["decision_json"])

    def approve(
        self,
        approval_request_id: str,
        *,
        authenticated_principal_reference: str,
        authorization_expires_at: datetime,
        decided_at: datetime,
    ) -> AuthorityAuthorization:
        """Atomically append approval and issue exactly one bound authorization."""
        require_aware(decided_at, "decided_at")
        require_aware(authorization_expires_at, "authorization_expires_at")
        with self.database.transaction(immediate=True) as connection:
            request = self._pending_request(connection, approval_request_id, decided_at)
            if authorization_expires_at <= decided_at or authorization_expires_at > request.approval_request.expires_at:
                raise ValueError("authorization expiry must be after approval and no later than request expiry")
            workflow = self._workflow(connection, request.workflow_id)
            if not self._matches_workflow(request, workflow):
                self._record_terminal_decision(
                    connection,
                    request,
                    ApprovalDecisionKind.INVALIDATED,
                    decided_at,
                    None,
                    "workflow_binding_changed",
                )
                raise AuthorityTerminalDecisionError("workflow bindings changed before approval")
            decision = AuthorityDecision(
                decision_id=str(uuid.uuid4()),
                approval_request_id=approval_request_id,
                authority_instance_id=self.authority_instance_id,
                decision_kind=ApprovalDecisionKind.APPROVED,
                decided_at=decided_at,
                authenticated_principal_reference=authenticated_principal_reference,
                reason_code="approved",
            )
            self._insert_decision(connection, decision)
            legacy = ExecutionAuthorization(
                execution_id=str(uuid.uuid4()),
                workflow_id=request.workflow_id,
                approval_request_id=approval_request_id,
                authenticated_principal=authenticated_principal_reference,
                proposal_digest=request.approval_request.proposal_digest,
                configuration_key=request.configuration_key,
                live_state_fingerprint=request.live_state_fingerprint,
                operation_type=request.operation_type,
                mutation_strategy=request.mutation_strategy,
                expected_workflow_version=request.expected_workflow_version,
                issued_at=decided_at,
                expires_at=authorization_expires_at,
                agent_identity=request.approval_request.agent_identity,
                client_identity=request.approval_request.client_identity,
            )
            authorization = AuthorityAuthorization(
                execution_authorization=legacy,
                authority_decision_id=decision.decision_id,
                authority_instance_id=self.authority_instance_id,
                preview_id=request.approval_request.preview_id,
                workspace_revision=request.workspace_revision,
                policy_versions=request.policy_versions,
            )
            connection.execute(
                """INSERT INTO authority_authorizations(
                execution_id, approval_request_id, workflow_id, authority_instance_id, state, authorization_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    legacy.execution_id,
                    approval_request_id,
                    request.workflow_id,
                    self.authority_instance_id,
                    AuthorizationState.ISSUED.value,
                    canonical_json(authorization),
                    _timestamp(decided_at),
                ),
            )
            self._insert_authorization_event(
                connection,
                legacy.execution_id,
                None,
                AuthorizationState.ISSUED,
                "issued",
                decided_at,
            )
        return authorization

    def reject(self, approval_request_id: str, *, decided_at: datetime, reason_code: str = "rejected") -> AuthorityDecision:
        return self._decide_terminal(
            approval_request_id,
            kind=ApprovalDecisionKind.REJECTED,
            decided_at=decided_at,
            reason_code=reason_code,
        )

    def cancel(self, approval_request_id: str, *, decided_at: datetime, reason_code: str = "cancelled") -> AuthorityDecision:
        return self._decide_terminal(
            approval_request_id,
            kind=ApprovalDecisionKind.CANCELLED,
            decided_at=decided_at,
            reason_code=reason_code,
        )

    def expire(self, approval_request_id: str, *, decided_at: datetime) -> AuthorityDecision:
        require_aware(decided_at, "decided_at")
        with self.database.transaction(immediate=True) as connection:
            request = self._request_in_transaction(connection, approval_request_id)
            existing = self._decision_in_transaction(connection, approval_request_id)
            if existing is not None:
                raise AuthorityTerminalDecisionError("approval request already has a terminal decision")
            if decided_at < request.approval_request.expires_at:
                raise ValueError("approval request has not expired")
            return self._record_terminal_decision(
                connection,
                request,
                ApprovalDecisionKind.EXPIRED,
                decided_at,
                None,
                "expired",
            )

    def authorization(self, execution_id: str) -> AuthorityAuthorization:
        require_uuid4(execution_id, "execution_id")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT authorization_json FROM authority_authorizations WHERE execution_id = ?", (execution_id,)
            ).fetchone()
        if row is None:
            raise AuthorityAuthorizationNotFoundError(execution_id)
        return from_json(AuthorityAuthorization, row["authorization_json"])

    def authorization_state(self, execution_id: str) -> AuthorizationState:
        require_uuid4(execution_id, "execution_id")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT state FROM authority_authorizations WHERE execution_id = ?", (execution_id,)
            ).fetchone()
        if row is None:
            raise AuthorityAuthorizationNotFoundError(execution_id)
        return AuthorizationState(row["state"])

    def invalidate_authorization(
        self, execution_id: str, *, occurred_at: datetime, reason_code: str = "invalidated"
    ) -> AuthorizationState:
        require_aware(occurred_at, "occurred_at")
        with self.database.transaction(immediate=True) as connection:
            row = self._authorization_row(connection, execution_id)
            current = AuthorizationState(row["state"])
            if current is not AuthorizationState.ISSUED:
                return current
            self._set_authorization_state(
                connection,
                execution_id,
                current,
                AuthorizationState.INVALIDATED,
                reason_code,
                occurred_at,
            )
        return AuthorizationState.INVALIDATED

    def admit(self, candidate: AuthorityAuthorization, *, admitted_at: datetime) -> AuthorizationAdmission:
        """Consume one authorization once; repeated calls return the original result.

        This performs only durable admission.  LEASE, REC intent, and operation
        creation must later share the larger execution transaction.
        """
        require_aware(admitted_at, "admitted_at")
        execution_id = candidate.execution_authorization.execution_id
        with self.database.transaction(immediate=True) as connection:
            prior = connection.execute(
                "SELECT admission_json FROM authority_admissions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if prior is not None:
                return from_json(AuthorizationAdmission, prior["admission_json"])
            row = self._authorization_row(connection, execution_id)
            persisted = from_json(AuthorityAuthorization, row["authorization_json"])
            current = AuthorizationState(row["state"])
            status, target_state, reason = self._admission_outcome(
                candidate,
                persisted,
                current,
                connection,
                admitted_at,
            )
            admission = AuthorizationAdmission(
                execution_id=execution_id,
                workflow_id=persisted.execution_authorization.workflow_id,
                status=status,
                reason_code=reason,
                recorded_at=admitted_at,
            )
            if target_state is not current:
                self._set_authorization_state(
                    connection,
                    execution_id,
                    current,
                    target_state,
                    reason,
                    admitted_at,
                )
            connection.execute(
                "INSERT INTO authority_admissions(execution_id, admission_json) VALUES (?, ?)",
                (execution_id, canonical_json(admission)),
            )
        return admission

    def mark_consumed(self, execution_id: str, *, occurred_at: datetime, reason_code: str = "consumed") -> AuthorizationState:
        """Record completion of an already-admitted execution without reissuing it."""
        require_aware(occurred_at, "occurred_at")
        with self.database.transaction(immediate=True) as connection:
            row = self._authorization_row(connection, execution_id)
            current = AuthorizationState(row["state"])
            if current is AuthorizationState.CONSUMING:
                self._set_authorization_state(
                    connection,
                    execution_id,
                    current,
                    AuthorizationState.CONSUMED,
                    reason_code,
                    occurred_at,
                )
                return AuthorizationState.CONSUMED
            return current

    def _decide_terminal(
        self,
        approval_request_id: str,
        *,
        kind: ApprovalDecisionKind,
        decided_at: datetime,
        reason_code: str,
    ) -> AuthorityDecision:
        require_aware(decided_at, "decided_at")
        with self.database.transaction(immediate=True) as connection:
            request = self._pending_request(connection, approval_request_id, decided_at)
            return self._record_terminal_decision(connection, request, kind, decided_at, None, reason_code)

    def _pending_request(
        self, connection: sqlite3.Connection, approval_request_id: str, decided_at: datetime
    ) -> AuthorityApprovalRequest:
        request = self._request_in_transaction(connection, approval_request_id)
        if self._decision_in_transaction(connection, approval_request_id) is not None:
            raise AuthorityTerminalDecisionError("approval request already has a terminal decision")
        if decided_at >= request.approval_request.expires_at:
            self._record_terminal_decision(
                connection,
                request,
                ApprovalDecisionKind.EXPIRED,
                decided_at,
                None,
                "expired",
            )
            raise AuthorityRequestExpiredError("approval request expired")
        return request

    @staticmethod
    def _workflow(connection: sqlite3.Connection, workflow_id: str) -> WorkflowRecord:
        row = connection.execute("SELECT record_json FROM workflow_records WHERE workflow_id = ?", (workflow_id,)).fetchone()
        if row is None:
            raise AuthorityApprovalRequestNotFoundError(f"workflow {workflow_id}")
        return from_json(WorkflowRecord, row["record_json"])

    @staticmethod
    def _matches_workflow(request: AuthorityApprovalRequest, workflow: WorkflowRecord) -> bool:
        return (
            workflow.state is WorkflowState.AWAITING_AUTHORIZATION
            and workflow.workflow_version == request.expected_workflow_version
            and workflow.configuration_key == request.configuration_key
            and workflow.proposal_digest == request.approval_request.proposal_digest
        )

    @classmethod
    def _require_request_matches_workflow(cls, request: AuthorityApprovalRequest, workflow: WorkflowRecord) -> None:
        if not cls._matches_workflow(request, workflow):
            raise ValueError("approval request must bind an awaiting, matching durable workflow")

    @staticmethod
    def _request_in_transaction(connection: sqlite3.Connection, approval_request_id: str) -> AuthorityApprovalRequest:
        require_uuid4(approval_request_id, "approval_request_id")
        row = connection.execute(
            "SELECT request_json FROM authority_approval_requests WHERE approval_request_id = ?", (approval_request_id,)
        ).fetchone()
        if row is None:
            raise AuthorityApprovalRequestNotFoundError(approval_request_id)
        return from_json(AuthorityApprovalRequest, row["request_json"])

    @staticmethod
    def _decision_in_transaction(connection: sqlite3.Connection, approval_request_id: str) -> AuthorityDecision | None:
        row = connection.execute(
            "SELECT decision_json FROM authority_decisions WHERE approval_request_id = ?", (approval_request_id,)
        ).fetchone()
        return None if row is None else from_json(AuthorityDecision, row["decision_json"])

    def _record_terminal_decision(
        self,
        connection: sqlite3.Connection,
        request: AuthorityApprovalRequest,
        kind: ApprovalDecisionKind,
        decided_at: datetime,
        principal_reference: str | None,
        reason_code: str,
    ) -> AuthorityDecision:
        decision = AuthorityDecision(
            decision_id=str(uuid.uuid4()),
            approval_request_id=request.approval_request.approval_request_id,
            authority_instance_id=self.authority_instance_id,
            decision_kind=kind,
            decided_at=decided_at,
            authenticated_principal_reference=principal_reference,
            reason_code=reason_code,
        )
        self._insert_decision(connection, decision)
        return decision

    @staticmethod
    def _insert_decision(connection: sqlite3.Connection, decision: AuthorityDecision) -> None:
        connection.execute(
            """INSERT INTO authority_decisions(
            decision_id, approval_request_id, decision_kind, decided_at, decision_json
            ) VALUES (?, ?, ?, ?, ?)""",
            (
                decision.decision_id,
                decision.approval_request_id,
                decision.decision_kind.value,
                _timestamp(decision.decided_at),
                canonical_json(decision),
            ),
        )

    @staticmethod
    def _authorization_row(connection: sqlite3.Connection, execution_id: str) -> sqlite3.Row:
        require_uuid4(execution_id, "execution_id")
        row = connection.execute(
            "SELECT state, authorization_json FROM authority_authorizations WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        if row is None:
            raise AuthorityAuthorizationNotFoundError(execution_id)
        return row

    def _admission_outcome(
        self,
        candidate: AuthorityAuthorization,
        persisted: AuthorityAuthorization,
        current: AuthorizationState,
        connection: sqlite3.Connection,
        admitted_at: datetime,
    ) -> tuple[AdmissionStatus, AuthorizationState, str]:
        authorization = persisted.execution_authorization
        if current is AuthorizationState.EXPIRED:
            return AdmissionStatus.EXPIRED, current, "expired"
        if current is AuthorizationState.INVALIDATED:
            return AdmissionStatus.INVALIDATED, current, "invalidated"
        if current is AuthorizationState.REJECTED:
            return AdmissionStatus.REJECTED, current, "rejected"
        if current is AuthorizationState.CANCELLED:
            return AdmissionStatus.CANCELLED, current, "cancelled"
        if current is not AuthorizationState.ISSUED:
            return AdmissionStatus.INVALIDATED, AuthorizationState.INVALIDATED, "unsafe_authorization_state"
        if candidate != persisted or persisted.authority_instance_id != self.authority_instance_id:
            return AdmissionStatus.INVALIDATED, AuthorizationState.INVALIDATED, "authorization_integrity_failed"
        if admitted_at >= authorization.expires_at:
            return AdmissionStatus.EXPIRED, AuthorizationState.EXPIRED, "expired"
        workflow = self._workflow(connection, authorization.workflow_id)
        request = self._request_in_transaction(connection, authorization.approval_request_id)
        if not self._matches_workflow(request, workflow):
            return AdmissionStatus.INVALIDATED, AuthorizationState.INVALIDATED, "workflow_binding_changed"
        return AdmissionStatus.ADMITTED, AuthorizationState.CONSUMING, "admitted"

    def _set_authorization_state(
        self,
        connection: sqlite3.Connection,
        execution_id: str,
        before: AuthorizationState,
        after: AuthorizationState,
        reason_code: str,
        occurred_at: datetime,
    ) -> None:
        cursor = connection.execute(
            "UPDATE authority_authorizations SET state = ?, updated_at = ? WHERE execution_id = ? AND state = ?",
            (after.value, _timestamp(occurred_at), execution_id, before.value),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("authorization state changed during durable admission")
        self._insert_authorization_event(connection, execution_id, before, after, reason_code, occurred_at)

    @staticmethod
    def _insert_authorization_event(
        connection: sqlite3.Connection,
        execution_id: str,
        before: AuthorizationState | None,
        after: AuthorizationState,
        reason_code: str,
        occurred_at: datetime,
    ) -> None:
        connection.execute(
            """INSERT INTO authority_authorization_events(
            event_id, execution_id, state_before, state_after, reason_code, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                execution_id,
                None if before is None else before.value,
                after.value,
                reason_code,
                _timestamp(occurred_at),
            ),
        )


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "ApprovalAuthorityStore",
    "AuthorityApprovalRequestNotFoundError",
    "AuthorityAuthorizationNotFoundError",
    "AuthorityRequestExpiredError",
    "AuthorityTerminalDecisionError",
]
