"""Platform-independent persistence primitives."""

from .database import DatabaseVersionError, SCHEMA_VERSION, SQLiteDatabase
from .execution_admission import (
    ExecutionAdmissionConflictError,
    ExecutionAdmissionCoordinator,
    ExecutionAdmissionRejectedError,
)
from .approval_store import (
    ApprovalAuthorityStore,
    AuthorityApprovalRequestNotFoundError,
    AuthorityAuthorizationNotFoundError,
    AuthorityRequestExpiredError,
    AuthorityTerminalDecisionError,
)
from .calculation_store import (
    CalculationContextMismatchError,
    CalculationNotFoundError,
    CalculationStore,
    build_calculation_overlays,
)
from .model_graph_store import GraphContextMismatchError, GraphSnapshotNotFoundError, ModelGraphStore
from .identity_store import (
    IdentityAmbiguousError,
    IdentityBinding,
    IdentityConflictError,
    IdentityNotFoundError,
    IdentityStore,
)
from .lease_store import (
    ContextLeaseStore,
    LeaseBusyError,
    LeaseFenceRejectedError,
    LeaseNotFoundError,
    LeaseStateConflictError,
    LeaseWorkflowVersionConflictError,
)
from .reconciliation_store import (
    ReconciliationClassificationError,
    ReconciliationIntentConflictError,
    ReconciliationIntentNotFoundError,
    ReconciliationObservationConflictError,
    ReconciliationStore,
)
from .operation_store import (
    IdempotencyConflictError,
    InvalidOperationTransitionError,
    OperationNotFoundError,
    OperationRecord,
    OperationState,
    OperationStore,
    TERMINAL_STATES,
    new_idempotency_key,
)
from .workflow_store import (
    WorkflowAlreadyExistsError,
    WorkflowIdempotencyConflictError,
    WorkflowNotFoundError,
    WorkflowStore,
    WorkflowVersionConflictError,
)

__all__ = [name for name in globals() if not name.startswith("_")]
