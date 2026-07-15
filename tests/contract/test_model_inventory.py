from __future__ import annotations

import unittest

from powerfactory_agent.domain import (
    AssetKind,
    AssetLookupRequest,
    ComponentListRequest,
    InventoryQuery,
    ModelSummaryRequest,
)
from powerfactory_agent.gateway.fake import DeterministicFakeGateway
from powerfactory_agent.operations import InventoryService


PROJECT = "project-fixture"


class ReadOnlyRecordingGateway:
    """Narrow wrapper proving the service uses only admitted read operations."""

    def __init__(self) -> None:
        self.base = DeterministicFakeGateway()
        self.calls: list[str] = []
        self.queries: list[InventoryQuery] = []

    def active_context(self):
        self.calls.append("active_context")
        return self.base.active_context()

    def inventory(self, query):
        self.calls.append("inventory")
        self.queries.append(query)
        return self.base.inventory(query)


class ModelInventoryContractTests(unittest.TestCase):
    def test_summary_uses_category_scoped_bounded_queries_only(self) -> None:
        gateway = ReadOnlyRecordingGateway()
        summary = InventoryService(gateway).summarize(
            ModelSummaryRequest(PROJECT, page_size=1, inventory_limit=100, sample_limit_per_kind=1)
        )

        self.assertEqual("active_context", gateway.calls[0])
        self.assertTrue(all(call in ("active_context", "inventory") for call in gateway.calls))
        self.assertTrue(gateway.queries)
        self.assertTrue(all(query.asset_kind is not None for query in gateway.queries))
        self.assertTrue(all(query.exact_project_key == PROJECT for query in gateway.queries))
        self.assertTrue(all(query.page_size == 1 for query in gateway.queries))
        self.assertEqual(6, summary.total_count)

    def test_component_listing_issues_exactly_one_inventory_call(self) -> None:
        gateway = ReadOnlyRecordingGateway()
        response = InventoryService(gateway).list_components(
            ComponentListRequest(AssetKind.LOAD, PROJECT, page_size=1)
        )

        self.assertEqual(("active_context", "inventory"), tuple(gateway.calls))
        self.assertEqual(AssetKind.LOAD, gateway.queries[0].asset_kind)
        self.assertEqual(1, len(response.items))

    def test_lookup_is_uuid_exact_and_category_scoped(self) -> None:
        gateway = ReadOnlyRecordingGateway()
        response = InventoryService(gateway).lookup(
            AssetLookupRequest(
                gateway.base.load_2.product_identity,
                AssetKind.LOAD,
                "ElmLod",
                PROJECT,
                page_size=1,
            )
        )

        self.assertEqual(gateway.base.load_2.product_identity, response.asset.product_identity)
        self.assertTrue(all(query.asset_kind is AssetKind.LOAD for query in gateway.queries))
        self.assertTrue(all(query.exact_project_key == PROJECT for query in gateway.queries))


if __name__ == "__main__":
    unittest.main()
