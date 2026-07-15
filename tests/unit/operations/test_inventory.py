from __future__ import annotations

from dataclasses import replace
import unittest

from powerfactory_agent.domain import (
    AssetKind,
    AssetLookupRequest,
    ComponentListRequest,
    ConfigurationKey,
    ExtractionRevision,
    IdentityLifecycleState,
    InventoryPage,
    ModelSummaryRequest,
    PageCursor,
)
from powerfactory_agent.gateway.fake import DeterministicFakeGateway
from powerfactory_agent.operations import InventoryService, InventoryServiceError, InventoryServiceErrorCode
from powerfactory_agent.serialization import canonical_json, from_json


PROJECT = "project-fixture"


class GatewayWrapper:
    def __init__(self) -> None:
        self.base = DeterministicFakeGateway()

    def active_context(self):
        return self.base.active_context()

    def inventory(self, query):
        return self.base.inventory(query)


class WarningWrapper(GatewayWrapper):
    def inventory(self, query):
        page = self.base.inventory(query)
        if query.asset_kind is not AssetKind.LOAD or query.cursor is not None:
            return page
        unsupported = replace(
            page.items[0],
            locator=replace(page.items[0].locator, object_class="ElmUnsupported"),
        )
        unresolved = replace(page.items[1], lifecycle_state=IdentityLifecycleState.UNRESOLVED)
        return InventoryPage(query=query, items=(unsupported, unresolved), next_cursor=None)


class DuplicateWrapper(GatewayWrapper):
    def inventory(self, query):
        page = self.base.inventory(query)
        if query.asset_kind is AssetKind.LOAD and query.cursor is None:
            return InventoryPage(query=query, items=page.items + (page.items[-1],), next_cursor=None)
        return page


class BindingMismatchWrapper(GatewayWrapper):
    def __init__(self, mismatch: str) -> None:
        super().__init__()
        self.mismatch = mismatch

    def inventory(self, query):
        page = self.base.inventory(query)
        if self.mismatch == "configuration":
            wrong_query = replace(
                query,
                configuration_key=ConfigurationKey("configuration-key:v1:sha256:" + "f" * 64),
            )
        else:
            wrong_query = replace(
                query,
                extraction_revision=ExtractionRevision(query.extraction_revision.scope_id, 99),
            )
        return InventoryPage(query=wrong_query, items=page.items, next_cursor=page.next_cursor)


class ProjectMismatchWrapper(GatewayWrapper):
    def inventory(self, query):
        page = self.base.inventory(query)
        if not page.items:
            return page
        item = page.items[0]
        mismatched = replace(
            item,
            project_key="other-project",
            locator=replace(
                item.locator,
                project_provenance=replace(item.locator.project_provenance, project_key="other-project"),
            ),
        )
        return InventoryPage(query=query, items=(mismatched,) + page.items[1:], next_cursor=page.next_cursor)


class CursorLoopWrapper(GatewayWrapper):
    LOOP = PageCursor("loop.loop")

    def inventory(self, query):
        page = self.base.inventory(replace(query, cursor=None))
        items = page.items if query.cursor is None else ()
        return InventoryPage(query=query, items=items, next_cursor=self.LOOP)


class ContextProjectWrapper(GatewayWrapper):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def active_context(self):
        context = self.base.active_context()
        if self.mode == "empty":
            return replace(context, assets=())
        item = context.assets[0]
        other_project = replace(
            item,
            project_key="other-project",
            locator=replace(
                item.locator,
                project_provenance=replace(item.locator.project_provenance, project_key="other-project"),
            ),
        )
        return replace(context, assets=(item, other_project))


class InventoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = DeterministicFakeGateway()
        self.service = InventoryService(self.gateway)

    def test_summary_is_snapshot_bound_bounded_and_deterministic(self) -> None:
        request = ModelSummaryRequest(PROJECT, page_size=1, sample_limit_per_kind=1)
        first = self.service.summarize(request)
        second = self.service.summarize(request)

        self.assertEqual(first, second)
        self.assertEqual(6, first.total_count)
        self.assertEqual(6, first.supported_count)
        self.assertEqual(0, first.unsupported_count)
        self.assertEqual(0, first.unresolved_count)
        self.assertEqual(tuple(sorted(AssetKind, key=lambda kind: kind.value)), tuple(x.asset_kind for x in first.categories))
        self.assertTrue(all(len(category.sample_references) <= 1 for category in first.categories))
        context = self.gateway.active_context()
        self.assertEqual(context.configuration_key, first.binding.configuration_key)
        self.assertEqual(context.extraction_revision, first.binding.extraction_revision)
        self.assertEqual(context.freshness, first.binding.freshness)

    def test_summary_reports_structured_unsupported_and_unresolved_warnings(self) -> None:
        summary = InventoryService(WarningWrapper()).summarize(
            ModelSummaryRequest(PROJECT, warning_example_limit=1)
        )

        self.assertEqual(6, summary.total_count)
        self.assertEqual(4, summary.supported_count)
        self.assertEqual(1, summary.unsupported_count)
        self.assertEqual(1, summary.unresolved_count)
        self.assertEqual(
            ("unresolved_identity", "unsupported_object_class"),
            tuple(warning.code.value for warning in summary.warnings),
        )
        self.assertTrue(all(warning.count == 1 and len(warning.examples) == 1 for warning in summary.warnings))

    def test_component_listing_is_one_bounded_snapshot_page(self) -> None:
        first = self.service.list_components(ComponentListRequest(AssetKind.LOAD, PROJECT, 1))
        self.assertEqual(1, len(first.items))
        self.assertIsNotNone(first.next_cursor)
        second = self.service.list_components(
            ComponentListRequest(AssetKind.LOAD, PROJECT, 1, first.next_cursor)
        )
        self.assertEqual(1, len(second.items))
        self.assertIsNone(second.next_cursor)
        self.assertLess(first.items[0].product_identity.value, second.items[0].product_identity.value)

    def test_exact_lookup_requires_uuid_kind_class_and_project(self) -> None:
        result = self.service.lookup(
            AssetLookupRequest(
                self.gateway.load_1.product_identity,
                AssetKind.LOAD,
                "ElmLod",
                PROJECT,
                page_size=1,
            )
        )
        self.assertEqual(self.gateway.load_1, result.asset)
        self.assertEqual(PROJECT, result.binding.exact_project_key)

        with self.assertRaises(InventoryServiceError) as class_error:
            self.service.lookup(
                AssetLookupRequest(self.gateway.load_1.product_identity, AssetKind.LOAD, "ElmTerm", PROJECT)
            )
        self.assertIs(class_error.exception.code, InventoryServiceErrorCode.CLASS_MISMATCH)

    def test_lookup_not_found_and_unresolved_fail_closed(self) -> None:
        with self.assertRaises(InventoryServiceError) as missing:
            self.service.lookup(
                AssetLookupRequest(self.gateway.area.product_identity, AssetKind.LOAD, "ElmLod", PROJECT)
            )
        self.assertIs(missing.exception.code, InventoryServiceErrorCode.NOT_FOUND)

        warning_gateway = WarningWrapper()
        with self.assertRaises(InventoryServiceError) as unresolved:
            InventoryService(warning_gateway).lookup(
                AssetLookupRequest(warning_gateway.base.load_2.product_identity, AssetKind.LOAD, "ElmLod", PROJECT)
            )
        self.assertIs(unresolved.exception.code, InventoryServiceErrorCode.UNRESOLVED_IDENTITY)

    def test_duplicate_product_ids_fail_closed(self) -> None:
        with self.assertRaises(InventoryServiceError) as raised:
            InventoryService(DuplicateWrapper()).summarize(ModelSummaryRequest(PROJECT))
        self.assertIs(raised.exception.code, InventoryServiceErrorCode.DUPLICATE_PRODUCT_IDENTITY)

    def test_configuration_and_revision_mismatch_fail_closed(self) -> None:
        for mismatch, expected in (
            ("configuration", InventoryServiceErrorCode.CONFIGURATION_MISMATCH),
            ("revision", InventoryServiceErrorCode.REVISION_MISMATCH),
        ):
            with self.subTest(mismatch=mismatch), self.assertRaises(InventoryServiceError) as raised:
                InventoryService(BindingMismatchWrapper(mismatch)).summarize(ModelSummaryRequest(PROJECT))
            self.assertIs(raised.exception.code, expected)

    def test_project_mismatch_and_cursor_loop_fail_closed(self) -> None:
        with self.assertRaises(InventoryServiceError) as project:
            InventoryService(ProjectMismatchWrapper()).summarize(ModelSummaryRequest(PROJECT))
        self.assertIs(project.exception.code, InventoryServiceErrorCode.PROJECT_MISMATCH)

        with self.assertRaises(InventoryServiceError) as cursor:
            InventoryService(CursorLoopWrapper()).summarize(ModelSummaryRequest(PROJECT))
        self.assertIs(cursor.exception.code, InventoryServiceErrorCode.CURSOR_LOOP)

    def test_missing_or_ambiguous_active_project_evidence_fails_closed(self) -> None:
        for mode in ("empty", "multiple"):
            with self.subTest(mode=mode), self.assertRaises(InventoryServiceError) as raised:
                InventoryService(ContextProjectWrapper(mode)).summarize(ModelSummaryRequest(PROJECT))
            self.assertIs(raised.exception.code, InventoryServiceErrorCode.INVALID_GATEWAY_RESPONSE)

    def test_summary_and_lookup_enforce_global_scan_bound(self) -> None:
        self.assertEqual(6, self.service.summarize(ModelSummaryRequest(PROJECT, inventory_limit=6)).total_count)
        with self.assertRaises(InventoryServiceError) as summary:
            self.service.summarize(ModelSummaryRequest(PROJECT, inventory_limit=5))
        self.assertIs(summary.exception.code, InventoryServiceErrorCode.BOUND_EXCEEDED)

        with self.assertRaises(InventoryServiceError) as lookup:
            self.service.lookup(
                AssetLookupRequest(
                    self.gateway.load_1.product_identity,
                    AssetKind.LOAD,
                    "ElmLod",
                    PROJECT,
                    page_size=1,
                    inventory_limit=1,
                )
            )
        self.assertIs(lookup.exception.code, InventoryServiceErrorCode.BOUND_EXCEEDED)

    def test_inventory_responses_round_trip_through_strict_serialization(self) -> None:
        values = (
            self.service.summarize(ModelSummaryRequest(PROJECT)),
            self.service.list_components(ComponentListRequest(AssetKind.LINE, PROJECT, 10)),
            self.service.lookup(
                AssetLookupRequest(self.gateway.line.product_identity, AssetKind.LINE, "ElmLne", PROJECT)
            ),
        )
        for value in values:
            with self.subTest(model=type(value).__name__):
                self.assertEqual(value, from_json(type(value), canonical_json(value)))


if __name__ == "__main__":
    unittest.main()
