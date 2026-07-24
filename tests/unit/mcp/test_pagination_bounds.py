"""Phase H: bounded pagination progress in supported-class extraction.

The snapshot extractor's _query_all_objects must reject repeated cursors,
fail closed on truncation without forward progress, and enforce a
conservative page/record ceiling. It must never silently truncate a snapshot
and label it complete, and the failure diagnostic must not contain raw vendor
data (cursor tokens or extracted records).

These tests drive the extractor directly with a recording fake owner and a
controllable await callable so the bounding logic is deterministic without
threads, SQLite, or PowerFactory.
"""

from __future__ import annotations

import unittest

from powerfactory_agent.domain import ObjectClassKind
from powerfactory_agent.mcp.runtime import RuntimeOperationFailure, _SupportedClassSnapshotExtractor


class _FakeRecord:
    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id


class _FakeBatch:
    def __init__(self, records, complete: bool, next_cursor: str | None = None) -> None:
        self.records = tuple(records)
        self.complete = complete
        self.next_cursor = next_cursor


class _FakeContext:
    def __init__(self) -> None:
        self.configuration_key = "fixture-configuration-key"


class _FakeOwner:
    def __init__(self) -> None:
        self.submissions = 0

    def submit_query_objects(self, request, *, idempotency_key) -> _FakeRecord:
        self.submissions += 1
        return _FakeRecord(f"op-{self.submissions}")

    def diagnostics(self) -> dict[str, object]:
        return {"quarantined": False, "stopping": False, "active_operation_id": None}


_ORDERED_CLASSES = (
    ObjectClassKind.GRID,
    ObjectClassKind.TERMINAL,
    ObjectClassKind.LINE,
    ObjectClassKind.LOAD,
    ObjectClassKind.TRANSFORMER,
)


def _extractor(batches, *, cardinality_ceiling=10_000):
    owner = _FakeOwner()
    iterator = iter(batches)

    def await_operation(_record, _result_type):
        return next(iterator)

    return _SupportedClassSnapshotExtractor(
        owner,
        identity_store=None,
        graph_store=None,
        installation_id="inst",
        profile_id="prof",
        await_operation=await_operation,
        cardinality_ceiling=cardinality_ceiling,
    )


def _complete(records=()) -> _FakeBatch:
    return _FakeBatch(records, complete=True, next_cursor=None)


class PaginationBoundsTests(unittest.TestCase):
    def test_normal_multi_page_results_are_preserved_in_order(self) -> None:
        # GRID: two pages then complete; remaining classes complete in one page.
        batches = [
            _FakeBatch(["g1", "g2"], complete=False, next_cursor="grid-c1"),
            _complete(["g3"]),
            _complete(["t1"]),
            _complete(["l1"]),
            _complete(["lo1"]),
            _complete(["tr1"]),
        ]
        extractor = _extractor(batches)

        records = extractor._query_all_objects(_FakeContext())

        self.assertEqual(("g1", "g2", "g3", "t1", "l1", "lo1", "tr1"), records)

    def test_repeated_cursor_is_rejected(self) -> None:
        batches = [
            _FakeBatch(["g1"], complete=False, next_cursor="grid-c1"),
            _FakeBatch(["g2"], complete=False, next_cursor="grid-c1"),
        ]
        extractor = _extractor(batches)

        with self.assertRaises(RuntimeOperationFailure) as ctx:
            extractor._query_all_objects(_FakeContext())
        self.assertEqual("repeated_cursor", ctx.exception.diagnostic["pagination"]["reason"])
        self.assertEqual("grid", ctx.exception.diagnostic["pagination"]["object_class"])
        self.assertNotIn("grid-c1", str(ctx.exception.diagnostic))

    def test_truncation_without_forward_progress_fails_closed(self) -> None:
        batches = [
            _FakeBatch(["g1"], complete=False, next_cursor=None),
        ]
        extractor = _extractor(batches)

        with self.assertRaises(RuntimeOperationFailure) as ctx:
            extractor._query_all_objects(_FakeContext())
        self.assertEqual("truncation_without_progress", ctx.exception.diagnostic["pagination"]["reason"])
        self.assertNotIn("records", ctx.exception.diagnostic)

    def test_page_ceiling_is_enforced(self) -> None:
        # cardinality_ceiling=50 -> page_ceiling = 50//100 + 1 = 1 page per class.
        batches = [
            _FakeBatch(["g1"], complete=False, next_cursor="grid-c1"),
        ]
        extractor = _extractor(batches, cardinality_ceiling=50)

        with self.assertRaises(RuntimeOperationFailure) as ctx:
            extractor._query_all_objects(_FakeContext())
        self.assertEqual("page_ceiling_exceeded", ctx.exception.diagnostic["pagination"]["reason"])

    def test_record_ceiling_is_enforced(self) -> None:
        # cardinality_ceiling=2; first class alone returns 3 records.
        batches = [_complete(["g1", "g2", "g3"])]
        extractor = _extractor(batches, cardinality_ceiling=2)

        with self.assertRaises(RuntimeOperationFailure) as ctx:
            extractor._query_all_objects(_FakeContext())
        self.assertEqual("record_ceiling_exceeded", ctx.exception.diagnostic["pagination"]["reason"])

    def test_diagnostic_excludes_raw_vendor_data(self) -> None:
        batches = [
            _FakeBatch(["secret-record"], complete=False, next_cursor="secret-cursor"),
            _FakeBatch(["secret-record"], complete=False, next_cursor="secret-cursor"),
        ]
        extractor = _extractor(batches)

        with self.assertRaises(RuntimeOperationFailure) as ctx:
            extractor._query_all_objects(_FakeContext())
        rendered = repr(ctx.exception.diagnostic)
        self.assertNotIn("secret-record", rendered)
        self.assertNotIn("secret-cursor", rendered)
        self.assertEqual(
            {"schema_version", "evidence_id", "pagination", "owner", "mcp_process"},
            set(ctx.exception.diagnostic),
        )


if __name__ == "__main__":
    unittest.main()