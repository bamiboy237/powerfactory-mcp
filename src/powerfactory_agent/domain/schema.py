"""Deterministic JSON Schema generation for domain contracts."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import json
from pathlib import Path
import types
from typing import Union, get_args, get_origin, get_type_hints

from . import approval, calculations, gateway, inventory, models, topology, values, workflow


SCHEMA_VERSION = "0.7.0"
SCHEMA_ID = f"https://powerfactory-agent.local/schemas/domain/{SCHEMA_VERSION}/domain.schema.json"
SCHEMA_PATH = Path("specs/schemas/domain/domain-v1.schema.json")


def _public_contract_types() -> tuple[type[object], ...]:
    admitted: list[type[object]] = []
    for module in (values, models, inventory, topology, gateway, calculations, workflow, approval):
        for name in module.__all__ if hasattr(module, "__all__") else dir(module):
            candidate = getattr(module, name)
            if (
                isinstance(candidate, type)
                and candidate.__module__ == module.__name__
                and not name.startswith("_")
                and (is_dataclass(candidate) or issubclass(candidate, Enum))
            ):
                admitted.append(candidate)
    return tuple(sorted(admitted, key=lambda item: item.__name__))


DOMAIN_TYPES = _public_contract_types()


def _type_schema(annotation: object) -> dict[str, object]:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        return {"oneOf": [_type_schema(argument) for argument in args]}
    if origin is tuple:
        if len(args) != 2 or args[1] is not Ellipsis:
            raise TypeError(f"unsupported tuple annotation: {annotation!r}")
        return {
            "type": "array",
            "items": _type_schema(args[0]),
            "maxItems": values.MAX_COLLECTION_LENGTH,
        }
    if annotation is type(None):
        return {"type": "null"}
    if annotation is datetime:
        return {"type": "string", "format": "date-time"}
    if annotation is Decimal:
        return {"type": "string", "pattern": "^-?(0|[1-9][0-9]*)(\\.[0-9]+)?$"}
    if annotation is str:
        return {"type": "string", "minLength": 1, "maxLength": values.MAX_TEXT_LENGTH}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if isinstance(annotation, type) and (is_dataclass(annotation) or issubclass(annotation, Enum)):
        return {"$ref": f"#/$defs/{annotation.__name__}"}
    raise TypeError(f"unsupported schema annotation: {annotation!r}")


def _definition(contract_type: type[object]) -> dict[str, object]:
    if issubclass(contract_type, Enum):
        return {
            "type": "string",
            "enum": [member.value for member in contract_type],
        }
    hints = get_type_hints(contract_type)
    properties = {field.name: _type_schema(hints[field.name]) for field in fields(contract_type)}
    definition: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": [field.name for field in fields(contract_type)],
    }
    if contract_type is values.ProductIdentity:
        properties["value"] = {
            "type": "string",
            "format": "uuid",
            "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        }
    if contract_type is values.ConfigurationKey:
        properties["value"] = {
            "type": "string",
            "pattern": "^configuration-key:v1:sha256:[0-9a-f]{64}$",
        }
    if issubclass(contract_type, values._DigestValue):
        properties["value"] = {
            "type": "string",
            "pattern": f"^{contract_type.DIGEST_KIND}:{contract_type.DIGEST_SCHEMA}:sha256:[0-9a-f]{{64}}$",
        }
    if issubclass(contract_type, values._Revision):
        properties["scope_id"] = {
            "type": "string",
            "format": "uuid",
            "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        }
        properties["counter"] = {"type": "integer", "minimum": 0}
    if contract_type is values.PowerFactoryLocator:
        properties["locator_version_id"] = {
            "type": "string",
            "format": "uuid",
            "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        }
    if contract_type is values.Quantity:
        properties["value"] = {"type": "string", "pattern": "^-?(0|[1-9][0-9]*)(\\.[0-9]+)?$"}
        properties["unit"] = {"type": "string", "minLength": 1, "maxLength": 64}
    if contract_type is models.InventoryQuery:
        properties["page_size"] = {"type": "integer", "minimum": 1, "maximum": values.MAX_PAGE_SIZE}
    if contract_type in (
        gateway.ObjectQueryRequest,
        gateway.DependencyReadRequest,
        gateway.ResultCollectionRequest,
    ):
        properties["limit"] = {"type": "integer", "minimum": 1, "maximum": values.MAX_PAGE_SIZE}
    if contract_type is gateway.LogReadRequest:
        properties["entry_limit"] = {"type": "integer", "minimum": 1, "maximum": values.MAX_PAGE_SIZE}
        properties["byte_limit"] = {"type": "integer", "minimum": 1, "maximum": gateway.MAX_LOG_BYTES}
    if contract_type is gateway.LogEntry:
        properties["sequence"] = {"type": "integer", "minimum": 0}
    if contract_type is gateway.LogBatch:
        properties["bytes_returned"] = {"type": "integer", "minimum": 0, "maximum": gateway.MAX_LOG_BYTES}
    if contract_type is gateway.GatewayWarning:
        properties["count"] = {"type": "integer", "minimum": 1}
    if contract_type is gateway.ObjectQueryBatch:
        properties["warnings"]["maxItems"] = gateway.MAX_GATEWAY_WARNINGS
    if contract_type in (inventory.ModelSummaryRequest, inventory.AssetLookupRequest):
        properties["page_size"] = {"type": "integer", "minimum": 1, "maximum": values.MAX_PAGE_SIZE}
        properties["inventory_limit"] = {
            "type": "integer",
            "minimum": 1,
            "maximum": values.MAX_COLLECTION_LENGTH,
        }
    if contract_type is inventory.ModelSummaryRequest:
        properties["sample_limit_per_kind"] = {
            "type": "integer",
            "minimum": 0,
            "maximum": inventory.MAX_INVENTORY_SAMPLES_PER_KIND,
        }
        properties["warning_example_limit"] = {
            "type": "integer",
            "minimum": 0,
            "maximum": inventory.MAX_INVENTORY_WARNING_EXAMPLES,
        }
    if contract_type is inventory.ComponentListRequest:
        properties["page_size"] = {"type": "integer", "minimum": 1, "maximum": values.MAX_PAGE_SIZE}
    if contract_type is inventory.ExtractionWarning:
        properties["count"] = {"type": "integer", "minimum": 1, "maximum": values.MAX_COLLECTION_LENGTH}
        properties["examples"]["maxItems"] = inventory.MAX_INVENTORY_WARNING_EXAMPLES
    if contract_type is inventory.AssetKindSummary:
        for field_name in ("total_count", "supported_count", "unsupported_count", "unresolved_count"):
            properties[field_name] = {
                "type": "integer",
                "minimum": 0,
                "maximum": values.MAX_COLLECTION_LENGTH,
            }
        properties["sample_references"]["maxItems"] = inventory.MAX_INVENTORY_SAMPLES_PER_KIND
    if contract_type is inventory.ModelSummary:
        for field_name in ("total_count", "supported_count", "unsupported_count", "unresolved_count"):
            properties[field_name] = {
                "type": "integer",
                "minimum": 0,
                "maximum": values.MAX_COLLECTION_LENGTH,
            }
    if contract_type is models.PageCursor:
        properties["token"] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096,
            "pattern": "^[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+$",
        }
    return definition


def generate_domain_schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "PowerFactory Agent Domain Contracts",
        "x-schema-version": SCHEMA_VERSION,
        "$defs": {contract_type.__name__: _definition(contract_type) for contract_type in DOMAIN_TYPES},
    }


def schema_text() -> str:
    return json.dumps(generate_domain_schema(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def write_schema(repository_root: Path) -> Path:
    destination = repository_root / SCHEMA_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(schema_text(), encoding="utf-8")
    return destination


def check_schema(repository_root: Path) -> bool:
    destination = repository_root / SCHEMA_PATH
    return destination.is_file() and destination.read_text(encoding="utf-8") == schema_text()
