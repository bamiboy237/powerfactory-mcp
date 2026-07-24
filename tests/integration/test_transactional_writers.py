"""Phase I: focused tests for the shared transaction-local writers.

These exercise the deduplicated helpers directly against a real SQLiteDatabase
schema. The retained lease/workflow/admission integration tests remain the
authoritative proof of atomic, ordered, idempotent transaction behavior; these
tests pin the shared SQL and timestamp encoding in isolation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from powerfactory_agent.domain import (
    ConfigurationKey,
    ContentDigest,
    LeaseMode,
    VersionedName,
    WorkflowRecord,
    WorkflowState,
    WorkflowVersion,
)
from powerfactory_agent.persistence import ContextLeaseStore, SQLiteDatabase, WorkflowStore
from powerfactory_agent.persistence._transactional_writers import (
    append_lease_event,
    encode_utc_timestamp,
    mint_fencing_token,
    write_lease_row,
)


WORKFLOW_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OWNER_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _digest(character: str) -> ContentDigest:
    return ContentDigest(f"content:v1:sha256:{character * 64}")


class _Setup(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.directory.name) / "writers.sqlite")
        self.scope = _digest("a")
        self.configuration = ConfigurationKey("configuration-key:v1:sha256:" + "b" * 64)
        WorkflowStore(self.database).record(
            WorkflowRecord(
                workflow_id=WORKFLOW_ID,
                state=WorkflowState.NEW,
                workflow_version=WorkflowVersion(WORKFLOW_ID, 0),
                operation_specification=VersionedName("area-load-scaling", "v1"),
                configuration_key=self.configuration,
                proposal_digest=_digest("c"),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        self.store = ContextLeaseStore(self.database)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _acquire_lease(self):
        return self.store.acquire(
            mode=LeaseMode.PREVIEW,
            service_scope_digest=self.scope,
            configuration_key=self.configuration,
            workflow_id=WORKFLOW_ID,
            expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 0),
            owner_instance_id=OWNER_ID,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            reason="workflow context admission",
            evidence_reference="evidence:v1:lease-admission",
        )


class EncodeUtcTimestampTests(unittest.TestCase):
    def test_encodes_aware_datetime_as_canonical_utc_with_z(self) -> None:
        aware_non_utc = datetime(2026, 7, 15, 14, 30, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual("2026-07-15T12:30:00Z", encode_utc_timestamp(aware_non_utc))

    def test_preserves_utc_isoformat_with_z_suffix(self) -> None:
        self.assertEqual(
            "2026-07-15T12:00:00Z",
            encode_utc_timestamp(datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)),
        )


class MintFencingTokenTests(_Setup):
    def test_increments_and_persists_per_scope(self) -> None:
        with self.database.transaction(immediate=True) as connection:
            first = mint_fencing_token(
                connection,
                service_scope_digest=self.scope,
                configuration_key=self.configuration,
            )
            second = mint_fencing_token(
                connection,
                service_scope_digest=self.scope,
                configuration_key=self.configuration,
            )

        self.assertEqual(1, first)
        self.assertEqual(2, second)


class WriteLeaseRowAndAppendLeaseEventTests(_Setup):
    def test_roundtrip_lease_and_event_through_shared_writers(self) -> None:
        lease = self._acquire_lease()
        events = self.store.lease_events(
            service_scope_digest=self.scope, configuration_key=self.configuration
        )
        self.assertTrue(events)

        # Write the lease and its first event into a fresh database using only the
        # shared writers, then read them back through the store's pure selectors.
        fresh = SQLiteDatabase(Path(self.directory.name) / "fresh.sqlite")
        WorkflowStore(fresh).record(
            WorkflowRecord(
                workflow_id=WORKFLOW_ID,
                state=WorkflowState.NEW,
                workflow_version=WorkflowVersion(WORKFLOW_ID, 0),
                operation_specification=VersionedName("area-load-scaling", "v1"),
                configuration_key=self.configuration,
                proposal_digest=_digest("c"),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        with fresh.transaction(immediate=True) as connection:
            mint_fencing_token(
                connection,
                service_scope_digest=self.scope,
                configuration_key=self.configuration,
            )
            write_lease_row(connection, lease)
            append_lease_event(connection, events[0])

        fresh_store = ContextLeaseStore(fresh)
        roundtripped = fresh_store.lease(
            service_scope_digest=self.scope, configuration_key=self.configuration
        )
        roundtripped_events = fresh_store.lease_events(
            service_scope_digest=self.scope, configuration_key=self.configuration
        )
        self.assertEqual(lease, roundtripped)
        self.assertEqual(events[0], roundtripped_events[0])


if __name__ == "__main__":
    unittest.main()