from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest

from powerfactory_agent.gateway.worker import (
    EngineQuarantinedError,
    KnownOperationFailure,
    OperationRequest,
    SerializedOperationWorker,
    WorkerClosedError,
)
from powerfactory_agent.persistence import OperationState, OperationStore, SQLiteDatabase


def wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not met before timeout")


class SerializedOperationWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database = SQLiteDatabase(Path(self.temporary_directory.name) / "operations.db")
        self.store = OperationStore(database)
        self.workers: list[SerializedOperationWorker] = []

    def tearDown(self) -> None:
        for worker in self.workers:
            worker.close(timeout_ms=1_000)
        self.temporary_directory.cleanup()

    def worker(self, handlers, **overrides) -> SerializedOperationWorker:
        settings = {
            "max_queue_size": 64,
            "queue_deadline_ms": 1_000,
            "client_response_deadline_ms": 500,
            "engine_health_threshold_ms": 1_000,
            "shutdown_drain_deadline_ms": 500,
            "watchdog_interval_ms": 2,
        }
        settings.update(overrides)
        worker = SerializedOperationWorker(self.store, handlers, **settings)
        self.workers.append(worker)
        return worker

    def test_fifo_execution_uses_one_stable_thread(self) -> None:
        release = threading.Event()
        order: list[int] = []
        thread_ids: list[int] = []

        def handler(payload: object) -> object:
            assert isinstance(payload, dict)
            order.append(payload["index"])
            thread_ids.append(threading.get_ident())
            if payload["index"] == 0:
                release.wait(1)
            return payload

        worker = self.worker({"test.record": handler})
        records = [
            worker.submit(
                OperationRequest("test.record", {"index": index}, f"fifo-{index}"), wait=False
            )
            for index in range(3)
        ]
        wait_until(lambda: worker.active_operation_id == records[0].operation_id)
        release.set()
        for record in records:
            self.assertTrue(worker.wait_for_terminal(record.operation_id, timeout_ms=1_000).terminal)
        self.assertEqual([0, 1, 2], order)
        self.assertEqual(1, len(set(thread_ids)))
        self.assertEqual(thread_ids[0], worker.owner_thread_id)

    def test_queue_deadline_cancels_only_before_start(self) -> None:
        release = threading.Event()

        def handler(payload: object) -> object:
            assert isinstance(payload, dict)
            if payload["block"]:
                release.wait(1)
            return payload

        worker = self.worker(
            {"test.block": handler},
            queue_deadline_ms=150,
            client_response_deadline_ms=500,
        )
        first = worker.submit(
            OperationRequest("test.block", {"block": True}, "queue-first"), wait=False
        )
        wait_until(lambda: worker.status(first.operation_id).state is OperationState.IN_FLIGHT)
        second = worker.submit(
            OperationRequest("test.block", {"block": False}, "queue-second"), wait=False
        )
        wait_until(
            lambda: worker.status(second.operation_id).state
            is OperationState.CANCELLED_BEFORE_START
        )
        release.set()
        self.assertEqual(
            OperationState.COMPLETED,
            worker.wait_for_terminal(first.operation_id, timeout_ms=1_000).state,
        )

    def test_client_timeout_does_not_cancel_started_work(self) -> None:
        release = threading.Event()

        def handler(payload: object) -> object:
            release.wait(1)
            return {"done": True}

        worker = self.worker(
            {"test.slow": handler},
            client_response_deadline_ms=25,
            engine_health_threshold_ms=500,
        )
        request = OperationRequest("test.slow", {}, "slow-1")
        submitted = worker.submit(request, wait=False)
        wait_until(lambda: worker.status(submitted.operation_id).state is OperationState.IN_FLIGHT)
        timed_out = worker.submit(request)
        self.assertEqual(OperationState.CLIENT_TIMED_OUT, timed_out.state)
        release.set()
        completed = worker.wait_for_terminal(timed_out.operation_id, timeout_ms=1_000)
        self.assertEqual(OperationState.COMPLETED_AFTER_CLIENT_TIMEOUT, completed.state)
        self.assertEqual({"done": True}, completed.result)

    def test_idempotent_resubmission_executes_once(self) -> None:
        calls = 0

        def handler(payload: object) -> object:
            nonlocal calls
            calls += 1
            return payload

        worker = self.worker({"test.echo": handler})
        request = OperationRequest("test.echo", {"value": 7}, "same-key")
        first = worker.submit(request)
        second = worker.submit(request)
        self.assertEqual(first.operation_id, second.operation_id)
        self.assertEqual(1, calls)

    def test_health_threshold_quarantines_and_reconciles_late_success(self) -> None:
        release = threading.Event()

        def handler(payload: object) -> object:
            release.wait(1)
            return {"late": True}

        worker = self.worker(
            {"test.block": handler},
            client_response_deadline_ms=500,
            engine_health_threshold_ms=25,
        )
        record = worker.submit(OperationRequest("test.block", {}, "health-1"), wait=False)
        wait_until(lambda: worker.status(record.operation_id).state is OperationState.ENGINE_UNRESPONSIVE)
        wait_until(lambda: worker.quarantined)
        with self.assertRaises(EngineQuarantinedError):
            worker.submit(OperationRequest("test.block", {}, "health-2"), wait=False)
        release.set()
        completed = worker.wait_for_terminal(record.operation_id, timeout_ms=1_000)
        self.assertEqual(OperationState.RECONCILIATION_REQUIRED, completed.state)
        self.assertEqual({"late": True}, completed.result)
        self.assertTrue(worker.quarantined)

    def test_handler_outcomes_fail_closed_by_effect_certainty(self) -> None:
        def known_failure(payload: object) -> object:
            raise KnownOperationFailure("validation failed before execution")

        def raises(payload: object) -> object:
            raise RuntimeError("confidential handler details")

        def raw_handle(payload: object) -> object:
            return object()

        worker = self.worker(
            {"test.known": known_failure, "test.raises": raises, "test.raw": raw_handle}
        )
        failed = worker.submit(OperationRequest("test.known", {}, "failure-0"))
        uncertain = worker.submit(OperationRequest("test.raises", {}, "failure-1"))
        rejected = worker.submit(OperationRequest("test.raw", {}, "failure-2"))
        self.assertEqual(OperationState.FAILED, failed.state)
        self.assertEqual(OperationState.RECONCILIATION_REQUIRED, uncertain.state)
        self.assertEqual(OperationState.RECONCILIATION_REQUIRED, rejected.state)
        self.assertNotIn("confidential", str(uncertain.error))
        self.assertEqual("unserializable_handler_outcome", rejected.error["category"])

    def test_known_failure_after_health_threshold_requires_reconciliation(self) -> None:
        release = threading.Event()

        def handler(payload: object) -> object:
            release.wait(1)
            raise KnownOperationFailure("no effect, but engine health was already lost")

        worker = self.worker(
            {"test.known_slow": handler},
            client_response_deadline_ms=500,
            engine_health_threshold_ms=25,
        )
        record = worker.submit(
            OperationRequest("test.known_slow", {}, "known-slow"), wait=False
        )
        wait_until(
            lambda: worker.status(record.operation_id).state is OperationState.ENGINE_UNRESPONSIVE
        )
        release.set()
        reconciled = worker.wait_for_terminal(record.operation_id, timeout_ms=1_000)
        self.assertEqual(OperationState.RECONCILIATION_REQUIRED, reconciled.state)

    def test_concurrent_submissions_never_overlap_handlers(self) -> None:
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0
        thread_ids: set[int] = set()

        def handler(payload: object) -> object:
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                thread_ids.add(threading.get_ident())
            time.sleep(0.003)
            with state_lock:
                active -= 1
            return payload

        worker = self.worker(
            {"test.concurrent": handler},
            queue_deadline_ms=5_000,
            client_response_deadline_ms=5_000,
            engine_health_threshold_ms=5_000,
        )

        def submit(index: int):
            return worker.submit(
                OperationRequest("test.concurrent", {"index": index}, f"concurrent-{index}")
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            records = list(executor.map(submit, range(30)))
        self.assertTrue(all(record.state is OperationState.COMPLETED for record in records))
        self.assertEqual(1, maximum_active)
        self.assertEqual({worker.owner_thread_id}, thread_ids)

    def test_clean_close_rejects_new_admission(self) -> None:
        worker = self.worker({"test.echo": lambda payload: payload})
        completed = worker.submit(OperationRequest("test.echo", {}, "close-1"))
        self.assertEqual(OperationState.COMPLETED, completed.state)
        self.assertTrue(worker.close(timeout_ms=500))
        with self.assertRaises(WorkerClosedError):
            worker.submit(OperationRequest("test.echo", {}, "close-2"), wait=False)


if __name__ == "__main__":
    unittest.main()
