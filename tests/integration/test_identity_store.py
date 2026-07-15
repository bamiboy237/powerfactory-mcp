from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from powerfactory_agent.domain import (
    ExtractionRevision,
    IdentityLifecycleState,
    IdentityTombstone,
    LocatorEvidenceSchema,
    LocatorKind,
    LocatorRebind,
    LocatorTrust,
    PowerFactoryLocator,
    ProductIdentity,
    ProjectProvenance,
)
from powerfactory_agent.persistence import IdentityConflictError, IdentityNotFoundError, IdentityStore, SQLiteDatabase


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
PRODUCT_ID = "11111111-1111-4111-8111-111111111111"
LOCATOR_ID = "22222222-2222-4222-8222-222222222222"
REPLACEMENT_ID = "33333333-3333-4333-8333-333333333333"
CONTEXT_ID = "44444444-4444-4444-8444-444444444444"


def locator(locator_id: str = LOCATOR_ID, path: str = "Grid/Load A.ElmLod") -> PowerFactoryLocator:
    return PowerFactoryLocator(
        locator_id,
        LocatorKind.CANONICAL_PATH_FALLBACK,
        ProjectProvenance("install-a", "profile-a", "Project.IntPrj", "probe:project-a"),
        "ElmLod",
        None,
        None,
        path,
        LocatorEvidenceSchema("powerfactory-locator", "v1", "powerfactory2026/0.1.0-unvalidated"),
        NOW,
        "session-a",
        LocatorTrust.FALLBACK,
    )


class IdentityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.temporary_directory.name) / "identity.db")
        self.store = IdentityStore(self.database, identity_factory=lambda: PRODUCT_ID)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_binding_survives_restart_and_locator_is_not_product_identity(self) -> None:
        created = self.store.create(locator(), evidence_reference="inventory:1")
        restarted = IdentityStore(self.database).resolve_exact(locator())
        self.assertEqual(ProductIdentity(PRODUCT_ID), created.product_identity)
        self.assertEqual(created, restarted)
        self.assertNotEqual(created.product_identity.value, created.current_locator.canonical_path)
        with self.assertRaises(IdentityConflictError):
            self.store.create(locator(), evidence_reference="inventory:duplicate")

    def test_transient_absence_is_unresolved_not_tombstoned(self) -> None:
        binding = self.store.create(locator(), evidence_reference="inventory:1")
        unresolved = self.store.mark_unresolved(
            binding.product_identity,
            observed_at=NOW + timedelta(seconds=1),
            evidence_reference="engine:unavailable",
        )
        self.assertEqual(IdentityLifecycleState.UNRESOLVED, unresolved.lifecycle_state)
        with self.assertRaises(IdentityNotFoundError):
            self.store.resolve_exact(locator())
        self.assertEqual(
            (IdentityLifecycleState.ACTIVE, IdentityLifecycleState.UNRESOLVED),
            tuple(item.state for item in self.store.lifecycle(binding.product_identity)),
        )

    def test_rebind_requires_native_equality_and_tombstone_never_resurrects(self) -> None:
        binding = self.store.create(locator(), evidence_reference="inventory:1")
        replacement = replace(
            locator(REPLACEMENT_ID, "Grid/Renamed Load.ElmLod"),
            locator_kind=LocatorKind.NATIVE_CANDIDATE,
            native_field="candidate_id",
            native_value="native-1",
            trust=LocatorTrust.VERIFIED_NATIVE,
            native_evidence_accepted=True,
        )
        rebound = self.store.rebind(
            LocatorRebind(binding.product_identity, locator(), replacement, True, "windows:native-equality"),
            observed_at=NOW + timedelta(seconds=1),
        )
        self.assertEqual(REPLACEMENT_ID, rebound.current_locator.locator_version_id)
        tombstoned = self.store.tombstone(
            IdentityTombstone(
                binding.product_identity,
                ExtractionRevision(CONTEXT_ID, 2),
                "complete inventory proves absence",
                NOW + timedelta(seconds=2),
                True,
            )
        )
        self.assertEqual(IdentityLifecycleState.TOMBSTONED, tombstoned.lifecycle_state)
        with self.assertRaises(IdentityConflictError):
            self.store.rebind(
                LocatorRebind(binding.product_identity, replacement, replace(replacement, locator_version_id=LOCATOR_ID), True, "late"),
                observed_at=NOW + timedelta(seconds=3),
            )


if __name__ == "__main__":
    unittest.main()
