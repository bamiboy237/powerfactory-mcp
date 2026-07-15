"""Bounded inventory application service over a narrow read-only gateway."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from powerfactory_agent.domain import (
    AssetKind,
    AssetReference,
    IdentityLifecycleState,
    InventoryPage,
    InventoryQuery,
    ModelContext,
    PageCursor,
)
from powerfactory_agent.domain.inventory import (
    AssetKindSummary,
    AssetLookupRequest,
    AssetLookupResult,
    ComponentListRequest,
    ComponentPage,
    ExtractionWarning,
    ExtractionWarningCode,
    InventoryBinding,
    ModelSummary,
    ModelSummaryRequest,
)


_SUPPORTED_OBJECT_CLASSES: dict[AssetKind, frozenset[str]] = {
    AssetKind.AREA: frozenset(("ElmArea",)),
    AssetKind.BUS: frozenset(("ElmTerm",)),
    AssetKind.LINE: frozenset(("ElmLne",)),
    AssetKind.LOAD: frozenset(("ElmLod",)),
    AssetKind.TERMINAL: frozenset(("ElmTerm",)),
    AssetKind.TRANSFORMER: frozenset(("ElmTr2", "ElmTr3")),
}


@runtime_checkable
class InventoryGateway(Protocol):
    """Smallest gateway surface required by read-only inventory operations."""

    def active_context(self) -> ModelContext: ...

    def inventory(self, query: InventoryQuery) -> InventoryPage: ...


class InventoryServiceErrorCode(str, Enum):
    BOUND_EXCEEDED = "bound_exceeded"
    CLASS_MISMATCH = "class_mismatch"
    CONFIGURATION_MISMATCH = "configuration_mismatch"
    CURSOR_LOOP = "cursor_loop"
    DUPLICATE_PRODUCT_IDENTITY = "duplicate_product_identity"
    INVALID_GATEWAY_RESPONSE = "invalid_gateway_response"
    NOT_FOUND = "not_found"
    PROJECT_MISMATCH = "project_mismatch"
    REVISION_MISMATCH = "revision_mismatch"
    UNRESOLVED_IDENTITY = "unresolved_identity"


class InventoryServiceError(RuntimeError):
    def __init__(self, code: InventoryServiceErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class _WarningAccumulator:
    count: int
    examples: list[str]


class InventoryService:
    """Builds bounded, deterministic views without exposing vendor objects."""

    def __init__(self, gateway: InventoryGateway) -> None:
        if not isinstance(gateway, InventoryGateway):
            raise TypeError("gateway must provide active_context() and inventory()")
        self._gateway = gateway

    def summarize(self, request: ModelSummaryRequest) -> ModelSummary:
        context = self._active_context(request.exact_project_key)
        binding = self._binding(context, request.exact_project_key)
        seen_product_ids: set[str] = set()
        warnings: dict[ExtractionWarningCode, _WarningAccumulator] = {}
        category_summaries: list[AssetKindSummary] = []
        scanned = 0

        for asset_kind in sorted(AssetKind, key=lambda kind: kind.value):
            total = supported = unsupported = unresolved = 0
            samples: list[AssetReference] = []
            query = InventoryQuery(
                configuration_key=context.configuration_key,
                extraction_revision=context.extraction_revision,
                asset_kind=asset_kind,
                exact_project_key=request.exact_project_key,
                page_size=request.page_size,
                cursor=None,
            )
            for item in self._scan(query, request.inventory_limit - scanned):
                scanned += 1
                self._admit_unique(item, seen_product_ids)
                self._validate_item_scope(item, asset_kind, request.exact_project_key)
                total += 1
                if len(samples) < request.sample_limit_per_kind:
                    samples.append(item)
                if item.lifecycle_state is not IdentityLifecycleState.ACTIVE:
                    unresolved += 1
                    self._warn(
                        warnings,
                        ExtractionWarningCode.UNRESOLVED_IDENTITY,
                        f"{item.product_identity.value}:{item.lifecycle_state.value}",
                        request.warning_example_limit,
                    )
                elif item.locator.object_class not in _SUPPORTED_OBJECT_CLASSES[asset_kind]:
                    unsupported += 1
                    self._warn(
                        warnings,
                        ExtractionWarningCode.UNSUPPORTED_OBJECT_CLASS,
                        f"{item.product_identity.value}:{item.locator.object_class}",
                        request.warning_example_limit,
                    )
                else:
                    supported += 1
            category_summaries.append(
                AssetKindSummary(
                    asset_kind=asset_kind,
                    total_count=total,
                    supported_count=supported,
                    unsupported_count=unsupported,
                    unresolved_count=unresolved,
                    sample_references=tuple(samples),
                )
            )

        categories = tuple(category_summaries)
        return ModelSummary(
            request=request,
            binding=binding,
            categories=categories,
            total_count=sum(item.total_count for item in categories),
            supported_count=sum(item.supported_count for item in categories),
            unsupported_count=sum(item.unsupported_count for item in categories),
            unresolved_count=sum(item.unresolved_count for item in categories),
            warnings=self._warnings(warnings),
        )

    def list_components(self, request: ComponentListRequest) -> ComponentPage:
        context = self._active_context(request.exact_project_key)
        query = InventoryQuery(
            configuration_key=context.configuration_key,
            extraction_revision=context.extraction_revision,
            asset_kind=request.asset_kind,
            exact_project_key=request.exact_project_key,
            page_size=request.page_size,
            cursor=request.cursor,
        )
        page = self._page(query)
        warnings: dict[ExtractionWarningCode, _WarningAccumulator] = {}
        seen: set[str] = set()
        for item in page.items:
            self._admit_unique(item, seen)
            self._validate_item_scope(item, request.asset_kind, request.exact_project_key)
            if item.lifecycle_state is not IdentityLifecycleState.ACTIVE:
                self._warn(
                    warnings,
                    ExtractionWarningCode.UNRESOLVED_IDENTITY,
                    f"{item.product_identity.value}:{item.lifecycle_state.value}",
                    3,
                )
            elif item.locator.object_class not in _SUPPORTED_OBJECT_CLASSES[request.asset_kind]:
                self._warn(
                    warnings,
                    ExtractionWarningCode.UNSUPPORTED_OBJECT_CLASS,
                    f"{item.product_identity.value}:{item.locator.object_class}",
                    3,
                )
        if request.cursor is not None and page.next_cursor == request.cursor:
            raise InventoryServiceError(InventoryServiceErrorCode.CURSOR_LOOP, "inventory cursor did not advance")
        return ComponentPage(
            request=request,
            binding=self._binding(context, request.exact_project_key),
            items=page.items,
            next_cursor=page.next_cursor,
            warnings=self._warnings(warnings),
        )

    def lookup(self, request: AssetLookupRequest) -> AssetLookupResult:
        supported_classes = _SUPPORTED_OBJECT_CLASSES[request.asset_kind]
        if request.exact_object_class not in supported_classes:
            raise InventoryServiceError(
                InventoryServiceErrorCode.CLASS_MISMATCH,
                "requested object class is not admitted for the asset kind",
            )
        context = self._active_context(request.exact_project_key)
        query = InventoryQuery(
            configuration_key=context.configuration_key,
            extraction_revision=context.extraction_revision,
            asset_kind=request.asset_kind,
            exact_project_key=request.exact_project_key,
            page_size=request.page_size,
            cursor=None,
        )
        seen: set[str] = set()
        found: AssetReference | None = None
        for item in self._scan(query, request.inventory_limit):
            self._admit_unique(item, seen)
            self._validate_item_scope(item, request.asset_kind, request.exact_project_key)
            if item.product_identity == request.product_identity:
                found = item
        if found is None:
            raise InventoryServiceError(InventoryServiceErrorCode.NOT_FOUND, "product UUID was not found in the exact scope")
        if found.lifecycle_state is not IdentityLifecycleState.ACTIVE:
            raise InventoryServiceError(
                InventoryServiceErrorCode.UNRESOLVED_IDENTITY,
                "product UUID is present but its identity is unresolved",
            )
        if found.locator.object_class != request.exact_object_class:
            raise InventoryServiceError(
                InventoryServiceErrorCode.CLASS_MISMATCH,
                "product UUID resolved to a different object class",
            )
        return AssetLookupResult(
            request=request,
            binding=self._binding(context, request.exact_project_key),
            asset=found,
        )

    def _active_context(self, exact_project_key: str) -> ModelContext:
        context = self._gateway.active_context()
        if not isinstance(context, ModelContext):
            raise InventoryServiceError(
                InventoryServiceErrorCode.INVALID_GATEWAY_RESPONSE,
                "active_context returned an invalid response",
            )
        project_keys = {item.project_key for item in context.assets}
        if not project_keys:
            raise InventoryServiceError(
                InventoryServiceErrorCode.INVALID_GATEWAY_RESPONSE,
                "active context does not contain project evidence",
            )
        if len(project_keys) != 1:
            raise InventoryServiceError(
                InventoryServiceErrorCode.INVALID_GATEWAY_RESPONSE,
                "active context contains ambiguous project evidence",
            )
        if project_keys != {exact_project_key}:
            raise InventoryServiceError(
                InventoryServiceErrorCode.PROJECT_MISMATCH,
                "requested project does not match the active context",
            )
        return context

    @staticmethod
    def _binding(context: ModelContext, exact_project_key: str) -> InventoryBinding:
        return InventoryBinding(
            configuration_key=context.configuration_key,
            extraction_revision=context.extraction_revision,
            freshness=context.freshness,
            exact_project_key=exact_project_key,
        )

    def _scan(self, initial_query: InventoryQuery, limit: int) -> Iterator[AssetReference]:
        if limit < 0:
            raise InventoryServiceError(
                InventoryServiceErrorCode.BOUND_EXCEEDED,
                "inventory traversal exceeded its configured item limit",
            )
        query = initial_query
        seen_cursors: set[str] = set()
        scanned = 0
        while True:
            page = self._page(query)
            for item in page.items:
                scanned += 1
                if scanned > limit:
                    raise InventoryServiceError(
                        InventoryServiceErrorCode.BOUND_EXCEEDED,
                        "inventory traversal exceeded its configured item limit",
                    )
                yield item
            if page.next_cursor is None:
                return
            token = page.next_cursor.token
            if token in seen_cursors or (query.cursor is not None and token == query.cursor.token):
                raise InventoryServiceError(InventoryServiceErrorCode.CURSOR_LOOP, "inventory cursor repeated")
            seen_cursors.add(token)
            query = InventoryQuery(
                configuration_key=query.configuration_key,
                extraction_revision=query.extraction_revision,
                asset_kind=query.asset_kind,
                exact_project_key=query.exact_project_key,
                page_size=query.page_size,
                cursor=PageCursor(token),
                sort_specification=query.sort_specification,
            )

    def _page(self, query: InventoryQuery) -> InventoryPage:
        page = self._gateway.inventory(query)
        if not isinstance(page, InventoryPage):
            raise InventoryServiceError(
                InventoryServiceErrorCode.INVALID_GATEWAY_RESPONSE,
                "inventory returned an invalid response",
            )
        if page.query.configuration_key != query.configuration_key:
            raise InventoryServiceError(
                InventoryServiceErrorCode.CONFIGURATION_MISMATCH,
                "inventory page is bound to another configuration",
            )
        if page.query.extraction_revision != query.extraction_revision:
            raise InventoryServiceError(
                InventoryServiceErrorCode.REVISION_MISMATCH,
                "inventory page is bound to another extraction revision",
            )
        if page.query != query:
            raise InventoryServiceError(
                InventoryServiceErrorCode.INVALID_GATEWAY_RESPONSE,
                "inventory page query binding does not match the request",
            )
        return page

    @staticmethod
    def _admit_unique(item: AssetReference, seen: set[str]) -> None:
        product_id = item.product_identity.value
        if product_id in seen:
            raise InventoryServiceError(
                InventoryServiceErrorCode.DUPLICATE_PRODUCT_IDENTITY,
                "inventory contains a duplicate product UUID",
            )
        seen.add(product_id)

    @staticmethod
    def _validate_item_scope(item: AssetReference, asset_kind: AssetKind, exact_project_key: str) -> None:
        if item.asset_kind is not asset_kind:
            raise InventoryServiceError(
                InventoryServiceErrorCode.CLASS_MISMATCH,
                "inventory item kind does not match the exact category query",
            )
        if item.project_key != exact_project_key or item.locator.project_provenance.project_key != exact_project_key:
            raise InventoryServiceError(
                InventoryServiceErrorCode.PROJECT_MISMATCH,
                "inventory item does not match the exact project query",
            )

    @staticmethod
    def _warn(
        warnings: dict[ExtractionWarningCode, _WarningAccumulator],
        code: ExtractionWarningCode,
        example: str,
        example_limit: int,
    ) -> None:
        warning = warnings.setdefault(code, _WarningAccumulator(0, []))
        warning.count += 1
        if len(warning.examples) < example_limit and example not in warning.examples:
            warning.examples.append(example)

    @staticmethod
    def _warnings(warnings: dict[ExtractionWarningCode, _WarningAccumulator]) -> tuple[ExtractionWarning, ...]:
        return tuple(
            ExtractionWarning(code=code, count=value.count, examples=tuple(value.examples))
            for code, value in sorted(warnings.items(), key=lambda item: item[0].value)
        )


__all__ = [
    "InventoryGateway",
    "InventoryService",
    "InventoryServiceError",
    "InventoryServiceErrorCode",
]
