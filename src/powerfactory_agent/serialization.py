"""Strict, deterministic JSON serialization for domain boundary values."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import math
import types
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

from .domain.values import MAX_COLLECTION_LENGTH, require_aware


MAX_SERIALIZED_BYTES = 1_048_576
CANONICALIZATION = "pf-agent-canonical-json/v1"
_DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
_TOKEN_PART_RE = re.compile(r"^[a-z][a-z0-9.-]*$")
T = TypeVar("T")


class SerializationError(ValueError):
    """Raised when a value cannot safely cross a product boundary."""


def _is_domain_dataclass(value: object) -> bool:
    return is_dataclass(value) and value.__class__.__module__ in {
        "powerfactory_agent.domain.gateway",
        "powerfactory_agent.domain.calculations",
        "powerfactory_agent.domain.approval",
        "powerfactory_agent.domain.inventory",
        "powerfactory_agent.domain.topology",
        "powerfactory_agent.domain.models",
        "powerfactory_agent.domain.values",
        "powerfactory_agent.domain.workflow",
        "powerfactory_agent.domain.lease",
    } and not value.__class__.__name__.startswith("_")


def to_primitive(value: object) -> object:
    """Convert admitted domain values to JSON-native values without fallback coercion."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return to_primitive(value.value)
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise SerializationError("JSON floating-point values are forbidden; use Decimal-backed Quantity")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise SerializationError("non-finite decimal values are not serializable")
        if value == 0:
            return "0"
        normalized = format(value, "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        if not _DECIMAL_RE.fullmatch(normalized):
            raise SerializationError("decimal cannot be represented canonically")
        return normalized
    if isinstance(value, datetime):
        require_aware(value, "datetime")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if _is_domain_dataclass(value):
        return {field.name: to_primitive(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple) or isinstance(value, list):
        if len(value) > MAX_COLLECTION_LENGTH:
            raise SerializationError("collection exceeds the domain boundary limit")
        return [to_primitive(item) for item in value]
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_LENGTH:
            raise SerializationError("mapping exceeds the domain boundary limit")
        if any(not isinstance(key, str) for key in value):
            raise SerializationError("JSON object keys must be strings")
        normalized: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise SerializationError("Unicode NFC normalization creates a duplicate object key")
            normalized[normalized_key] = to_primitive(item)
        return normalized
    raise SerializationError(f"unsupported boundary type: {type(value).__name__}")


def canonical_json(value: object, *, maximum_bytes: int = MAX_SERIALIZED_BYTES) -> str:
    if maximum_bytes < 1:
        raise ValueError("maximum_bytes must be positive")
    encoded = json.dumps(
        to_primitive(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise SerializationError(f"serialized payload exceeds {maximum_bytes} bytes")
    return encoded


def canonical_digest(value: object, *, kind: str = "content", schema: str = "v1") -> str:
    if not _TOKEN_PART_RE.fullmatch(kind) or not _TOKEN_PART_RE.fullmatch(schema):
        raise ValueError("digest kind and schema must be lowercase token components")
    envelope = {
        "canonicalization": CANONICALIZATION,
        "digest_kind": kind,
        "digest_schema": schema,
        "payload": value,
    }
    digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
    return f"{kind}:{schema}:sha256:{digest}"


def _decode(annotation: object, value: object, path: str) -> object:
    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in (Union, types.UnionType):
        errors: list[str] = []
        for candidate in args:
            try:
                return _decode(candidate, value, path)
            except (SerializationError, TypeError, ValueError) as exc:
                errors.append(str(exc))
        raise SerializationError(f"{path} does not match any admitted union member: {'; '.join(errors)}")

    if origin is tuple:
        if not isinstance(value, list):
            raise SerializationError(f"{path} must be a JSON array")
        if len(value) > MAX_COLLECTION_LENGTH:
            raise SerializationError(f"{path} exceeds the collection limit")
        if len(args) != 2 or args[1] is not Ellipsis:
            raise SerializationError(f"{path} uses an unsupported tuple annotation")
        return tuple(_decode(args[0], item, f"{path}[{index}]") for index, item in enumerate(value))

    if annotation is type(None):
        if value is not None:
            raise SerializationError(f"{path} must be null")
        return None
    if annotation is datetime:
        if not isinstance(value, str):
            raise SerializationError(f"{path} must be an RFC 3339 timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SerializationError(f"{path} is not a valid timestamp") from exc
        require_aware(parsed, path)
        return parsed
    if annotation is Decimal:
        if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value):
            raise SerializationError(f"{path} must be a canonical non-exponent decimal string")
        try:
            parsed_decimal = Decimal(value)
        except InvalidOperation as exc:
            raise SerializationError(f"{path} is not a valid decimal") from exc
        if not parsed_decimal.is_finite():
            raise SerializationError(f"{path} must be finite")
        return parsed_decimal
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        try:
            return annotation(value)
        except (TypeError, ValueError) as exc:
            raise SerializationError(f"{path} is not a valid {annotation.__name__}") from exc
    if isinstance(annotation, type) and is_dataclass(annotation):
        if annotation.__module__ not in {
            "powerfactory_agent.domain.gateway",
            "powerfactory_agent.domain.calculations",
            "powerfactory_agent.domain.approval",
            "powerfactory_agent.domain.inventory",
            "powerfactory_agent.domain.topology",
            "powerfactory_agent.domain.models",
            "powerfactory_agent.domain.values",
            "powerfactory_agent.domain.workflow",
            "powerfactory_agent.domain.lease",
        } or annotation.__name__.startswith("_"):
            raise SerializationError(f"{path} is not an admitted domain model")
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise SerializationError(f"{path} must be a JSON object")
        type_fields = fields(annotation)
        expected = {field.name for field in type_fields}
        actual = set(value)
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise SerializationError(f"{path} field mismatch; missing={missing}, unexpected={unexpected}")
        hints = get_type_hints(annotation)
        kwargs = {
            field.name: _decode(hints[field.name], value[field.name], f"{path}.{field.name}")
            for field in type_fields
        }
        try:
            return annotation(**kwargs)
        except (TypeError, ValueError) as exc:
            raise SerializationError(f"{path} failed domain validation: {exc}") from exc
    if annotation is str:
        if not isinstance(value, str):
            raise SerializationError(f"{path} must be a string")
        return value
    if annotation is bool:
        if not isinstance(value, bool):
            raise SerializationError(f"{path} must be a boolean")
        return value
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SerializationError(f"{path} must be an integer")
        return value
    if annotation is float:
        raise SerializationError(f"{path} uses forbidden binary floating-point")
    raise SerializationError(f"{path} uses unsupported annotation {annotation!r}")


def from_primitive(model_type: type[T], value: object) -> T:
    decoded = _decode(model_type, value, model_type.__name__)
    if not isinstance(decoded, model_type):
        raise SerializationError(f"decoded value is not {model_type.__name__}")
    return decoded


def from_json(model_type: type[T], payload: str, *, maximum_bytes: int = MAX_SERIALIZED_BYTES) -> T:
    if not isinstance(payload, str):
        raise TypeError("payload must be a string")
    if len(payload.encode("utf-8")) > maximum_bytes:
        raise SerializationError(f"serialized payload exceeds {maximum_bytes} bytes")
    try:
        value = json.loads(payload, parse_constant=lambda token: (_ for _ in ()).throw(
            SerializationError(f"invalid numeric constant: {token}")
        ))
    except json.JSONDecodeError as exc:
        raise SerializationError("payload is not valid JSON") from exc
    return from_primitive(model_type, value)
