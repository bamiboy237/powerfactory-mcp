"""Platform-independent persistence primitives."""

from .database import DatabaseVersionError, SCHEMA_VERSION, SQLiteDatabase
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

__all__ = [name for name in globals() if not name.startswith("_")]
