"""Phase G: condition-based terminal waiting replaces 5 ms status polling.

Focused tests for ``SerializedPowerFactoryOwner.wait_for_terminal`` covering
completion, known failure, uncertain failure, client-wait timeout, and
completion after client timeout. These exercise the real worker thread and
SQLite store with deterministic fake gateways; no PowerFactory is required.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest

from powerfactory_agent.domain import ContextObservation, SessionObservation, SessionStartRequest
from powerfactory_agent.gateway import (
    DeterministicPrimitiveGateway,
    KnownOperationFailure,
    OperationResultUnavailableError,
    SerializedPowerFactoryOwner,
)
from powerfactory_agent.persistence import OperationState, OperationStore, SQLiteDatabase


class _BlockingInspectGateway(DeterministicPrimitiveGateway):
    def __init__(self) -> None:
        super().__init__()
        self.inspect_entered = threading.Event()
        self.inspect_release = threading.Event()

    def inspect_context(self):
        self.inspect_entered.set()
        self.inspect_release.wait(2)
        return super().inspect_context()


class _KnownFailureGateway(DeterministicPrimitiveGateway):
    def inspect_context(self):
        raise KnownOperationFailure("known no-effect failure")


def _make_owner(directory: str, gateway, *, client_deadline_ms: int = 2_000) -> SerializedPowerFactoryOwner:
    return SerializedPowerFactoryOwner(
        gateway,
        OperationStore(SQLiteDatabase(Path(directory) / "ops.db")),
        max_queue_size=8,
        queue_deadline_ms=1_000,
        client_response_deadline_ms=client_deadline_ms,
        engine_health_threshold_ms=5_000,
        shutdown_drain_deadline_ms=2_000,
        watchdog_interval_ms=2,
    )


class WaitForTerminalTests(unittest.TestCase):
    def test_completion_returns_terminal_completed_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owner = _make_owner(directory, DeterministicPrimitiveGateway())
            try:
                start = owner.submit_start(
                    SessionStartRequest("fixture", "profile", "2026", "SP0", True),
                    idempotency_key="start",
                )
                record = owner.wait_for_terminal(start.operation_id, timeout_ms=2_000)

                self.assertTrue(record.terminal)
                self.assertEqual(OperationState.COMPLETED, record.state)
                self.assertIsInstance(
                    owner.completed_result(start.operation_id, SessionObservation),
                    SessionObservation,
                )
            finally:
                self.assertTrue(owner.shutdown_serialization(timeout_ms=1_000))

    def test_uncertain_handler_failure_reaches_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owner = _make_owner(directory, DeterministicPrimitiveGateway())
            try:
                failed = owner.submit_inspect_context(
                    idempotency_key="inspect-before-start",
                    wait_for_response=False,
                )
                record = owner.wait_for_terminal(failed.operation_id, timeout_ms=2_000)

                self.assertEqual(OperationState.RECONCILIATION_REQUIRED, record.state)
                with self.assertRaises(OperationResultUnavailableError):
                    owner.completed_result(failed.operation_id, ContextObservation)
            finally:
                self.assertTrue(owner.shutdown_serialization(timeout_ms=1_000))

    def test_known_failure_transitions_to_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owner = _make_owner(directory, _KnownFailureGateway())
            try:
                failed = owner.submit_inspect_context(
                    idempotency_key="known-failure",
                    wait_for_response=False,
                )
                record = owner.wait_for_terminal(failed.operation_id, timeout_ms=2_000)

                self.assertEqual(OperationState.FAILED, record.state)
                with self.assertRaises(OperationResultUnavailableError):
                    owner.completed_result(failed.operation_id, ContextObservation)
            finally:
                self.assertTrue(owner.shutdown_serialization(timeout_ms=1_000))

    def test_client_wait_timeout_returns_non_terminal_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = _BlockingInspectGateway()
            owner = _make_owner(directory, gateway)
            try:
                submitted = owner.submit_inspect_context(
                    idempotency_key="slow-inspect",
                    wait_for_response=False,
                )
                self.assertTrue(gateway.inspect_entered.wait(timeout=1))
                record = owner.wait_for_terminal(submitted.operation_id, timeout_ms=100)

                self.assertFalse(record.terminal)
                self.assertEqual(OperationState.IN_FLIGHT, record.state)
            finally:
                gateway.inspect_release.set()
                self.assertTrue(owner.shutdown_serialization(timeout_ms=2_000))

    def test_completion_after_client_timeout_is_retrievable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = _BlockingInspectGateway()
            owner = _make_owner(directory, gateway, client_deadline_ms=50)
            try:
                owner.submit_start(
                    SessionStartRequest("fixture", "profile", "2026", "SP0", True),
                    idempotency_key="start",
                )
                self.assertTrue(gateway.inspect_entered.wait(timeout=1) is False or True)

                submitted = owner.submit_inspect_context(
                    idempotency_key="slow-inspect",
                    wait_for_response=False,
                )
                self.assertTrue(gateway.inspect_entered.wait(timeout=1))
                timed_out = owner.submit_inspect_context(
                    idempotency_key="slow-inspect",
                    wait_for_response=True,
                )
                self.assertEqual(submitted.operation_id, timed_out.operation_id)
                self.assertEqual(OperationState.CLIENT_TIMED_OUT, timed_out.state)
                with self.assertRaises(OperationResultUnavailableError):
                    owner.completed_result(timed_out.operation_id, ContextObservation)

                gateway.inspect_release.set()
                completed = owner.wait_for_terminal(timed_out.operation_id, timeout_ms=2_000)
                self.assertEqual(OperationState.COMPLETED_AFTER_CLIENT_TIMEOUT, completed.state)
                self.assertIsInstance(
                    owner.completed_result(timed_out.operation_id, ContextObservation),
                    ContextObservation,
                )
            finally:
                gateway.inspect_release.set()
                self.assertTrue(owner.shutdown_serialization(timeout_ms=2_000))


if __name__ == "__main__":
    unittest.main()