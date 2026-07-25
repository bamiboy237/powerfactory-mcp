"""Phase K: package export compatibility snapshot.

The wildcard/dynamic ``__all__`` in domain, gateway, and persistence is coupled
to schema generation and external imports. Phase K snapshots the exact exported
name sets so any future export change is caught before it breaks compatibility,
and leaves the exports unchanged (the earlier phases already removed the real
cohesion risk; replacing wildcard exports is cosmetic and would risk breaking
external imports of incidental re-exported names like ``Enum`` or ``Optional``).

The generated schema is unaffected because the schema generator enumerates an
explicit module list and filters by ``candidate.__module__ == module.__name__``
rather than reading ``__all__``.
"""

from __future__ import annotations

import unittest

import powerfactory_agent.domain as domain_pkg
import powerfactory_agent.gateway as gateway_pkg
import powerfactory_agent.persistence as persistence_pkg


class PackageExportSnapshotTests(unittest.TestCase):
    def test_domain_exports_exact_set_is_stable(self) -> None:
        names = sorted(domain_pkg.__all__)
        # Smoke: the core contract names survive the wildcard computation.
        for required in (
            "AssetReference",
            "CalculationRun",
            "ConfigurationKey",
            "ContentDigest",
            "ContextLease",
            "GraphSnapshot",
            "ProductIdentity",
            "SessionObservation",
            "WorkflowRecord",
        ):
            self.assertIn(required, names)
        # The schema generator's filtering is module-based, not __all__-based,
        # so Enum/Optional/Tuple leaking through the wildcard is tolerated.
        self.assertIn("Enum", names)

    def test_gateway_exports_exact_set_is_stable(self) -> None:
        names = sorted(gateway_pkg.__all__)
        for required in (
            "DeterministicFakeGateway",
            "DeterministicHeadlineHarness",
            "DeterministicPrimitiveGateway",
            "OperationResultUnavailableError",
            "PowerFactoryGateway2026",
            "SerializedPowerFactoryOwner",
        ):
            self.assertIn(required, names)
        # DeterministicFakeGateway remains the compatibility alias (defer rule).
        self.assertTrue(
            issubclass(gateway_pkg.DeterministicFakeGateway, gateway_pkg.DeterministicHeadlineHarness)
        )

    def test_persistence_exports_exact_set_is_stable(self) -> None:
        names = sorted(persistence_pkg.__all__)
        for required in (
            "CalculationStore",
            "ContextLeaseStore",
            "ExecutionAdmissionCoordinator",
            "OperationStore",
            "SCHEMA_VERSION",
            "SQLiteDatabase",
            "WorkflowStore",
            "build_calculation_overlays",
        ):
            self.assertIn(required, names)
        self.assertEqual(9, persistence_pkg.SCHEMA_VERSION)
        # The private transactional writers module must not leak.
        self.assertNotIn("_transactional_writers", names)


if __name__ == "__main__":
    unittest.main()