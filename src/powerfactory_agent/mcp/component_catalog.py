"""Single admitted PowerFactory inventory mapping for the friend-test MCP surface."""

from __future__ import annotations

from powerfactory_agent.domain import AssetKind, ObjectClassKind


SUPPORTED_CLASS_ASSET_KINDS = (
    (ObjectClassKind.GRID, AssetKind.AREA),
    (ObjectClassKind.TERMINAL, AssetKind.TERMINAL),
    (ObjectClassKind.LINE, AssetKind.LINE),
    (ObjectClassKind.LOAD, AssetKind.LOAD),
    (ObjectClassKind.TRANSFORMER, AssetKind.TRANSFORMER),
)
SUPPORTED_OBJECT_CLASSES = tuple(item[0] for item in SUPPORTED_CLASS_ASSET_KINDS)
SUPPORTED_COMPONENT_ASSET_KINDS = tuple(item[1] for item in SUPPORTED_CLASS_ASSET_KINDS)
ADMITTED_COMPONENT_ASSET_KINDS = tuple(item.value for item in SUPPORTED_COMPONENT_ASSET_KINDS)


__all__ = [
    "ADMITTED_COMPONENT_ASSET_KINDS",
    "SUPPORTED_CLASS_ASSET_KINDS",
    "SUPPORTED_COMPONENT_ASSET_KINDS",
    "SUPPORTED_OBJECT_CLASSES",
]
