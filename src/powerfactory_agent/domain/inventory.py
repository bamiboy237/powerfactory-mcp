"""Serializable contracts for bounded read-only model inventory operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from .models import AssetReference, PageCursor
from .values import (
    MAX_COLLECTION_LENGTH,
    MAX_PAGE_SIZE,
    AssetKind,
    ConfigurationKey,
    ExtractionRevision,
    FreshnessEvidence,
    ProductIdentity,
    require_collection,
    require_text,
)


MAX_INVENTORY_WARNING_EXAMPLES = 20
MAX_INVENTORY_SAMPLES_PER_KIND = 20


def _require_bounded_int(value: int, field_name: str, *, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")


class ExtractionWarningCode(str, Enum):
    UNSUPPORTED_OBJECT_CLASS = "unsupported_object_class"
    UNRESOLVED_IDENTITY = "unresolved_identity"


@dataclass(frozen=True, slots=True)
class ExtractionWarning:
    code: ExtractionWarningCode
    count: int
    examples: Tuple[str, ...]

    def __post_init__(self) -> None:
        _require_bounded_int(self.count, "ExtractionWarning.count", minimum=1, maximum=MAX_COLLECTION_LENGTH)
        require_collection(self.examples, "ExtractionWarning.examples")
        if len(self.examples) > MAX_INVENTORY_WARNING_EXAMPLES:
            raise ValueError("ExtractionWarning.examples exceeds the hard boundary limit")
        for example in self.examples:
            require_text(example, "ExtractionWarning.examples entry", maximum=512)
        if len(set(self.examples)) != len(self.examples):
            raise ValueError("ExtractionWarning.examples must be unique")


@dataclass(frozen=True, slots=True)
class InventoryBinding:
    configuration_key: ConfigurationKey
    extraction_revision: ExtractionRevision
    freshness: FreshnessEvidence
    exact_project_key: str

    def __post_init__(self) -> None:
        require_text(self.exact_project_key, "InventoryBinding.exact_project_key", maximum=512)
        if self.freshness.configuration_key != self.configuration_key:
            raise ValueError("inventory freshness must bind the configuration key")


@dataclass(frozen=True, slots=True)
class ModelSummaryRequest:
    exact_project_key: str
    page_size: int = MAX_PAGE_SIZE
    inventory_limit: int = MAX_COLLECTION_LENGTH
    sample_limit_per_kind: int = 3
    warning_example_limit: int = 3

    def __post_init__(self) -> None:
        require_text(self.exact_project_key, "ModelSummaryRequest.exact_project_key", maximum=512)
        _require_bounded_int(self.page_size, "ModelSummaryRequest.page_size", minimum=1, maximum=MAX_PAGE_SIZE)
        _require_bounded_int(
            self.inventory_limit,
            "ModelSummaryRequest.inventory_limit",
            minimum=1,
            maximum=MAX_COLLECTION_LENGTH,
        )
        _require_bounded_int(
            self.sample_limit_per_kind,
            "ModelSummaryRequest.sample_limit_per_kind",
            minimum=0,
            maximum=MAX_INVENTORY_SAMPLES_PER_KIND,
        )
        _require_bounded_int(
            self.warning_example_limit,
            "ModelSummaryRequest.warning_example_limit",
            minimum=0,
            maximum=MAX_INVENTORY_WARNING_EXAMPLES,
        )


@dataclass(frozen=True, slots=True)
class AssetKindSummary:
    asset_kind: AssetKind
    total_count: int
    supported_count: int
    unsupported_count: int
    unresolved_count: int
    sample_references: Tuple[AssetReference, ...]

    def __post_init__(self) -> None:
        for field_name in ("total_count", "supported_count", "unsupported_count", "unresolved_count"):
            _require_bounded_int(
                getattr(self, field_name),
                f"AssetKindSummary.{field_name}",
                minimum=0,
                maximum=MAX_COLLECTION_LENGTH,
            )
        if self.total_count != self.supported_count + self.unsupported_count + self.unresolved_count:
            raise ValueError("asset-kind counts must partition the total")
        require_collection(self.sample_references, "AssetKindSummary.sample_references")
        if len(self.sample_references) > MAX_INVENTORY_SAMPLES_PER_KIND:
            raise ValueError("AssetKindSummary.sample_references exceeds the hard boundary limit")
        identities = tuple(item.product_identity.value for item in self.sample_references)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
            raise ValueError("sample references must have unique product UUID ordering")
        if any(item.asset_kind is not self.asset_kind for item in self.sample_references):
            raise ValueError("sample references must match the summarized asset kind")


@dataclass(frozen=True, slots=True)
class ModelSummary:
    request: ModelSummaryRequest
    binding: InventoryBinding
    categories: Tuple[AssetKindSummary, ...]
    total_count: int
    supported_count: int
    unsupported_count: int
    unresolved_count: int
    warnings: Tuple[ExtractionWarning, ...]

    def __post_init__(self) -> None:
        if self.binding.exact_project_key != self.request.exact_project_key:
            raise ValueError("model summary request and binding project must match")
        require_collection(self.categories, "ModelSummary.categories", allow_empty=False)
        expected_kinds = tuple(sorted(AssetKind, key=lambda kind: kind.value))
        actual_kinds = tuple(category.asset_kind for category in self.categories)
        if actual_kinds != expected_kinds:
            raise ValueError("model summary must contain every admitted asset kind in deterministic order")
        if any(len(category.sample_references) > self.request.sample_limit_per_kind for category in self.categories):
            raise ValueError("model summary sample count exceeds the requested limit")
        if any(
            sample.project_key != self.binding.exact_project_key
            for category in self.categories
            for sample in category.sample_references
        ):
            raise ValueError("model summary samples must match the exact project binding")
        for field_name in ("total_count", "supported_count", "unsupported_count", "unresolved_count"):
            _require_bounded_int(
                getattr(self, field_name),
                f"ModelSummary.{field_name}",
                minimum=0,
                maximum=self.request.inventory_limit,
            )
            if getattr(self, field_name) != sum(getattr(category, field_name) for category in self.categories):
                raise ValueError(f"ModelSummary.{field_name} must equal the category sum")
        if self.total_count != self.supported_count + self.unsupported_count + self.unresolved_count:
            raise ValueError("model summary counts must partition the total")
        require_collection(self.warnings, "ModelSummary.warnings")
        warning_codes = tuple(warning.code.value for warning in self.warnings)
        if warning_codes != tuple(sorted(warning_codes)) or len(set(warning_codes)) != len(warning_codes):
            raise ValueError("model summary warnings must have unique deterministic code ordering")
        if any(len(warning.examples) > self.request.warning_example_limit for warning in self.warnings):
            raise ValueError("model summary warning examples exceed the requested limit")
        warning_counts = {warning.code: warning.count for warning in self.warnings}
        expected_warning_counts = {
            code: count
            for code, count in (
                (ExtractionWarningCode.UNSUPPORTED_OBJECT_CLASS, self.unsupported_count),
                (ExtractionWarningCode.UNRESOLVED_IDENTITY, self.unresolved_count),
            )
            if count
        }
        if warning_counts != expected_warning_counts:
            raise ValueError("model summary warning counts must match unsupported and unresolved totals")


@dataclass(frozen=True, slots=True)
class ComponentListRequest:
    asset_kind: AssetKind
    exact_project_key: str
    page_size: int
    cursor: Optional[PageCursor] = None

    def __post_init__(self) -> None:
        require_text(self.exact_project_key, "ComponentListRequest.exact_project_key", maximum=512)
        _require_bounded_int(self.page_size, "ComponentListRequest.page_size", minimum=1, maximum=MAX_PAGE_SIZE)


@dataclass(frozen=True, slots=True)
class ComponentPage:
    request: ComponentListRequest
    binding: InventoryBinding
    items: Tuple[AssetReference, ...]
    next_cursor: Optional[PageCursor]
    warnings: Tuple[ExtractionWarning, ...]

    def __post_init__(self) -> None:
        if self.binding.exact_project_key != self.request.exact_project_key:
            raise ValueError("component request and binding project must match")
        require_collection(self.items, "ComponentPage.items")
        if len(self.items) > self.request.page_size:
            raise ValueError("component page exceeds the requested page size")
        identities = tuple(item.product_identity.value for item in self.items)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
            raise ValueError("component page items must have unique product UUID ordering")
        if any(item.asset_kind is not self.request.asset_kind for item in self.items):
            raise ValueError("component page item kind must match the request")
        if any(item.project_key != self.binding.exact_project_key for item in self.items):
            raise ValueError("component page items must match the exact project binding")
        require_collection(self.warnings, "ComponentPage.warnings")
        warning_codes = tuple(warning.code.value for warning in self.warnings)
        if warning_codes != tuple(sorted(warning_codes)) or len(set(warning_codes)) != len(warning_codes):
            raise ValueError("component page warnings must have unique deterministic code ordering")


@dataclass(frozen=True, slots=True)
class AssetLookupRequest:
    product_identity: ProductIdentity
    asset_kind: AssetKind
    exact_object_class: str
    exact_project_key: str
    page_size: int = MAX_PAGE_SIZE
    inventory_limit: int = MAX_COLLECTION_LENGTH

    def __post_init__(self) -> None:
        require_text(self.exact_object_class, "AssetLookupRequest.exact_object_class", maximum=128)
        require_text(self.exact_project_key, "AssetLookupRequest.exact_project_key", maximum=512)
        _require_bounded_int(self.page_size, "AssetLookupRequest.page_size", minimum=1, maximum=MAX_PAGE_SIZE)
        _require_bounded_int(
            self.inventory_limit,
            "AssetLookupRequest.inventory_limit",
            minimum=1,
            maximum=MAX_COLLECTION_LENGTH,
        )


@dataclass(frozen=True, slots=True)
class AssetLookupResult:
    request: AssetLookupRequest
    binding: InventoryBinding
    asset: AssetReference

    def __post_init__(self) -> None:
        if self.binding.exact_project_key != self.request.exact_project_key:
            raise ValueError("lookup request and binding project must match")
        if self.asset.product_identity != self.request.product_identity:
            raise ValueError("lookup result product UUID must match exactly")
        if self.asset.asset_kind is not self.request.asset_kind:
            raise ValueError("lookup result asset kind must match exactly")
        if self.asset.locator.object_class != self.request.exact_object_class:
            raise ValueError("lookup result object class must match exactly")
        if self.asset.project_key != self.request.exact_project_key:
            raise ValueError("lookup result project must match exactly")


__all__ = [name for name in globals() if not name.startswith("_")]
