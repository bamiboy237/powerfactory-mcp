from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from powerfactory_agent.persistence import (
    InvalidOperationTransitionError,
    OperationState,
    OperationStore,
    SQLiteDatabase,
)
from powerfactory_agent.serialization import SerializationError


class OperationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.temporary_directory.name) / "operations.db")
        self.store = OperationStore(self.database, maximum_json_bytes=512)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def admit(self, key: str = "request-1"):
        return self.store.admit(
            handler_name="test.echo",
            payload={"value": 1},
            idempotency_key=key,
            queue_deadline_ms=1_000,
            client_response_deadline_ms=2_000,
            engine_health_threshold_ms=3_000,
        )[0]

    def test_database_enables_required_pragmas_and_schema_version(self) -> None:
        self.assertEqual("wal", str(self.database.pragma("journal_mode")).lower())
        self.assertEqual(1, self.database.pragma("foreign_keys"))
        self.assertEqual(5_000, self.database.pragma("busy_timeout"))
        self.assertEqual(4, self.database.pragma("user_version"))

    def test_atomic_transition_allows_only_one_concurrent_claim(self) -> None:
        record = self.admit()

        def claim() -> object:
            try:
                return self.store.start(record.operation_id).state
            except InvalidOperationTransitionError:
                return "lost"

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(lambda _: claim(), range(8)))
        self.assertEqual(1, outcomes.count(OperationState.IN_FLIGHT))
        self.assertEqual(7, outcomes.count("lost"))

    def test_payload_result_and_error_are_bounded_strict_json(self) -> None:
        with self.assertRaises(SerializationError):
            self.store.admit(
                handler_name="test.echo",
                payload={"bad": object()},
                idempotency_key="bad-payload",
                queue_deadline_ms=1,
                client_response_deadline_ms=1,
                engine_health_threshold_ms=1,
            )
        record = self.admit()
        self.store.start(record.operation_id)
        with self.assertRaises(SerializationError):
            self.store.complete(record.operation_id, {"large": "x" * 1_000})
        self.assertEqual(OperationState.IN_FLIGHT, self.store.get(record.operation_id).state)

    def test_late_success_after_health_loss_requires_reconciliation(self) -> None:
        record = self.admit()
        self.store.start(record.operation_id)
        self.store.mark_engine_unresponsive(record.operation_id)
        reconciled = self.store.complete(record.operation_id, {"late": True})
        self.assertEqual(OperationState.RECONCILIATION_REQUIRED, reconciled.state)
        self.assertEqual({"late": True}, reconciled.result)


if __name__ == "__main__":
    unittest.main()
