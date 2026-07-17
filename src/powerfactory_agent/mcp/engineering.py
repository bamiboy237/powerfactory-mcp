"""Application boundary consumed by the thin MCP engineering tool surface."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .configuration import McpInstallation
from .component_catalog import ADMITTED_COMPONENT_ASSET_KINDS


def validate_component_list_request(*, asset_kind: str, limit: int) -> None:
    """Reject unsupported inventory requests before starting the native runtime."""

    if asset_kind not in ADMITTED_COMPONENT_ASSET_KINDS:
        supported = ", ".join(ADMITTED_COMPONENT_ASSET_KINDS)
        raise ValueError(
            f"asset_kind '{asset_kind}' is unavailable in this release; supported kinds: {supported}"
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")


@runtime_checkable
class EngineeringToolRuntime(Protocol):
    """High-level operations exposed by MCP without vendor objects or handles."""

    def activate_context(
        self, *, project_selector: str, study_case: str
    ) -> dict[str, object]: ...

    def get_model_context(self) -> dict[str, object]: ...

    def list_components(
        self,
        *,
        asset_kind: str,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]: ...

    def get_asset_context(self, *, product_identity: str) -> dict[str, object]: ...

    def run_validated_load_flow(self, *, idempotency_key: str) -> dict[str, object]: ...

    def get_calculation_run(self, *, run_id: str) -> dict[str, object]: ...

    def compare_results(
        self,
        *,
        baseline_snapshot_id: str,
        candidate_snapshot_id: str,
    ) -> dict[str, object]: ...

    def refresh_model_graph(self) -> dict[str, object]: ...

    def get_model_graph_summary(self) -> dict[str, object]: ...

    def query_model_graph(
        self,
        *,
        query_kind: str,
        model_context_id: str,
        extraction_revision: int,
        limit: int,
        center_identity: str | None,
        source_identity: str | None,
        target_identity: str | None,
        hops: int,
    ) -> dict[str, object]: ...

    def close(self) -> None: ...


def build_engineering_runtime(installation: McpInstallation) -> EngineeringToolRuntime:
    """Build the production runtime lazily on the first engineering tool call."""

    from .runtime import PowerFactoryEngineeringRuntime

    return PowerFactoryEngineeringRuntime(installation)


__all__ = [
    "ADMITTED_COMPONENT_ASSET_KINDS",
    "EngineeringToolRuntime",
    "build_engineering_runtime",
    "validate_component_list_request",
]
