"""Structured, vendor-independent gateway failure categories."""

from __future__ import annotations

from enum import Enum
import re
from typing import ClassVar, Mapping

from powerfactory_agent.domain import AppliedChange


MAX_ERROR_MESSAGE_LENGTH = 256
MAX_ERROR_DETAIL_COUNT = 16
MAX_ERROR_DETAIL_LENGTH = 256
_SAFE_DETAIL_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "cookie",
    "credential",
    "licence",
    "license",
    "password",
    "path",
    "secret",
    "token",
)
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b(password|secret|token|credential|authorization)\s*[:=]\s*\S+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_ABSOLUTE_PATH = re.compile(r"(?<!\w)(?:[A-Za-z]:\\\\|/)(?:[^\s/\\\\]+[/\\\\])+[^\s]*")


def _safe_text(value: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError("gateway error text must be a string")
    redacted = _ASSIGNMENT_SECRET.sub(lambda match: f"{match.group(1)}=[redacted]", value)
    redacted = _BEARER.sub("Bearer [redacted]", redacted)
    redacted = _ABSOLUTE_PATH.sub("[path]", redacted)
    redacted = " ".join(redacted.split())
    if not redacted:
        redacted = "gateway operation failed"
    return redacted[:maximum]


class GatewayErrorCategory(str, Enum):
    CONNECTION_FAILURE = "connection_failure"
    CONFIGURATION_MISMATCH = "configuration_mismatch"
    OBJECT_NOT_FOUND = "object_not_found"
    OBJECT_AMBIGUOUS = "object_ambiguous"
    STALE_CONTEXT = "stale_context"
    INVALID_OPERATION = "invalid_operation"
    AUTHORIZATION_FAILURE = "authorization_failure"
    AUTHORIZATION_REQUIRED = "authorization_required"
    AUTHORIZATION_INVALID = "authorization_invalid"
    CALCULATION_NON_CONVERGENCE = "calculation_non_convergence"
    PARTIAL_MUTATION = "partial_mutation"
    ROLLBACK_CONFLICT = "rollback_conflict"
    LEASE_LOST = "lease_lost"
    OPERATION_STILL_IN_FLIGHT = "operation_still_in_flight"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CURSOR_INVALID = "cursor_invalid"
    CURSOR_STALE = "cursor_stale"


class GatewayError(RuntimeError):
    """Base failure carrying a stable category and bounded scalar details."""

    category: ClassVar[GatewayErrorCategory]

    def __init__(self, message: str, *, details: Mapping[str, str] | None = None) -> None:
        safe_message = _safe_text(message, maximum=MAX_ERROR_MESSAGE_LENGTH)
        raw_details = details or {}
        if not isinstance(raw_details, Mapping):
            raise TypeError("gateway error details must be a mapping")
        if len(raw_details) > MAX_ERROR_DETAIL_COUNT:
            raise ValueError("gateway error details exceed the configured bound")
        safe_details: list[tuple[str, str]] = []
        for key, value in raw_details.items():
            if not isinstance(key, str) or not _SAFE_DETAIL_KEY.fullmatch(key):
                raise ValueError("gateway error detail keys must use safe snake_case")
            if any(fragment in key for fragment in _SENSITIVE_KEY_FRAGMENTS):
                raise ValueError("gateway error detail key is sensitive")
            if not isinstance(value, str):
                raise TypeError("gateway error detail values must be strings")
            safe_details.append((key, _safe_text(value, maximum=MAX_ERROR_DETAIL_LENGTH)))
        super().__init__(safe_message)
        self.message = safe_message
        self.details = tuple(sorted(safe_details))

    def to_record(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "message": self.message,
            "details": dict(self.details),
        }


class ConnectionFailure(GatewayError):
    category = GatewayErrorCategory.CONNECTION_FAILURE


class ConfigurationMismatch(GatewayError):
    category = GatewayErrorCategory.CONFIGURATION_MISMATCH


class ObjectNotFound(GatewayError):
    category = GatewayErrorCategory.OBJECT_NOT_FOUND


class ObjectAmbiguous(GatewayError):
    category = GatewayErrorCategory.OBJECT_AMBIGUOUS


class StaleContext(GatewayError):
    category = GatewayErrorCategory.STALE_CONTEXT


class InvalidOperation(GatewayError):
    category = GatewayErrorCategory.INVALID_OPERATION


class AuthorizationFailure(GatewayError):
    category = GatewayErrorCategory.AUTHORIZATION_FAILURE


class AuthorizationRequired(GatewayError):
    category = GatewayErrorCategory.AUTHORIZATION_REQUIRED


class AuthorizationInvalid(GatewayError):
    category = GatewayErrorCategory.AUTHORIZATION_INVALID


class CalculationNonConvergence(GatewayError):
    category = GatewayErrorCategory.CALCULATION_NON_CONVERGENCE


class PartialMutation(GatewayError):
    category = GatewayErrorCategory.PARTIAL_MUTATION

    def __init__(self, message: str, *, applied_change: AppliedChange) -> None:
        if not isinstance(applied_change, AppliedChange):
            raise TypeError("applied_change must be an AppliedChange")
        super().__init__(message)
        self.applied_change = applied_change


class RollbackConflictError(GatewayError):
    category = GatewayErrorCategory.ROLLBACK_CONFLICT


class LeaseLost(GatewayError):
    category = GatewayErrorCategory.LEASE_LOST


class OperationStillInFlight(GatewayError):
    category = GatewayErrorCategory.OPERATION_STILL_IN_FLIGHT


class ReconciliationRequired(GatewayError):
    category = GatewayErrorCategory.RECONCILIATION_REQUIRED


class CursorInvalid(GatewayError):
    """Cursor authentication, format, or query binding failed."""

    category = GatewayErrorCategory.CURSOR_INVALID


class CursorStale(GatewayError):
    """Cursor was authentic but its bounded lifetime expired."""

    category = GatewayErrorCategory.CURSOR_STALE


__all__ = [name for name in globals() if not name.startswith("_")]
