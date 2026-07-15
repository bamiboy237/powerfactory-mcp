from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from powerfactory_agent.gateway.worker import SerializedOperationWorker
from powerfactory_agent.persistence import OperationState, OperationStore, SQLiteDatabase


class OperationRecoveryTests(unittest.TestCase):
    def test_restart_reconciles_orphaned_calls_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "operations.db"
            store = OperationStore(SQLiteDatabase(database_path))
            records = []
            for index in range(3):
                record, _ = store.admit(
                    handler_name="test.never_replay",
                    payload={"index": index},
                    idempotency_key=f"orphan-{index}",
                    queue_deadline_ms=1_000,
                    client_response_deadline_ms=1_000,
                    engine_health_threshold_ms=1_000,
                )
                store.start(record.operation_id)
                records.append(record)
            store.mark_client_timed_out(records[1].operation_id)
            store.mark_engine_unresponsive(records[2].operation_id)
            queued, _ = store.admit(
                handler_name="test.never_replay",
                payload={"queued": True},
                idempotency_key="queued-on-restart",
                queue_deadline_ms=1_000,
                client_response_deadline_ms=1_000,
                engine_health_threshold_ms=1_000,
            )

            calls = 0

            def handler(payload: object) -> object:
                nonlocal calls
                calls += 1
                return payload

            restarted_store = OperationStore(SQLiteDatabase(database_path))
            worker = SerializedOperationWorker(
                restarted_store,
                {"test.never_replay": handler},
                max_queue_size=10,
                queue_deadline_ms=1_000,
                client_response_deadline_ms=1_000,
                engine_health_threshold_ms=1_000,
            )
            try:
                for record in records:
                    self.assertEqual(
                        OperationState.RECONCILIATION_REQUIRED,
                        worker.status(record.operation_id).state,
                    )
                self.assertEqual(
                    OperationState.CANCELLED_BEFORE_START,
                    worker.status(queued.operation_id).state,
                )
                self.assertEqual(0, calls)
            finally:
                self.assertTrue(worker.close(timeout_ms=500))


if __name__ == "__main__":
    unittest.main()
