"""Strict settings loaders for the platform-independent application core."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import math
import os
import re
from typing import Callable

from .models import (
    AgentSettings,
    ByteCount,
    EntryCount,
    PercentagePointDelta,
    PerUnitDelta,
    Seconds,
)


ENVIRONMENT_PREFIX = "POWERFACTORY_AGENT_"

_INTEGER_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_NUMBER_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")


def _parse_integer(value: object, key: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{key} must be an integer, not a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER_RE.fullmatch(value):
        return int(value)
    raise TypeError(f"{key} must be a nonnegative base-10 integer")


def _parse_number(value: object, key: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{key} must be numeric, not a boolean")
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str) and _NUMBER_RE.fullmatch(value):
        parsed = float(value)
    else:
        raise TypeError(f"{key} must be a nonnegative finite number")
    if not math.isfinite(parsed):
        raise ValueError(f"{key} must be finite")
    return parsed


def _entry_count(value: object, key: str) -> EntryCount:
    return EntryCount(_parse_integer(value, key))


def _byte_count(value: object, key: str) -> ByteCount:
    return ByteCount(_parse_integer(value, key))


def _seconds(value: object, key: str) -> Seconds:
    return Seconds(_parse_number(value, key))


def _per_unit(value: object, key: str) -> PerUnitDelta:
    return PerUnitDelta(_parse_number(value, key))


def _percentage_points(value: object, key: str) -> PercentagePointDelta:
    return PercentagePointDelta(_parse_number(value, key))


_PARSERS: dict[str, Callable[[object, str], object]] = {
    "cached_freshness_max_age_seconds": _seconds,
    "verified_freshness_max_age_seconds": _seconds,
    "inventory_default_page_size_entries": _entry_count,
    "inventory_max_page_size_entries": _entry_count,
    "maximum_collection_entries": _entry_count,
    "maximum_serialized_payload_bytes": _byte_count,
    "voltage_materiality_per_unit": _per_unit,
    "loading_materiality_percentage_points": _percentage_points,
    "queue_wait_timeout_seconds": _seconds,
    "client_response_timeout_seconds": _seconds,
    "engine_call_timeout_seconds": _seconds,
    "startup_timeout_seconds": _seconds,
    "shutdown_timeout_seconds": _seconds,
    "fake_gateway_cardinality_entries": _entry_count,
}

_FIELD_NAMES = {
    "cached_freshness_max_age_seconds": "cached_freshness_max_age",
    "verified_freshness_max_age_seconds": "verified_freshness_max_age",
    "inventory_default_page_size_entries": "inventory_default_page_size",
    "inventory_max_page_size_entries": "inventory_max_page_size",
    "maximum_collection_entries": "maximum_collection_entries",
    "maximum_serialized_payload_bytes": "maximum_serialized_payload",
    "voltage_materiality_per_unit": "voltage_materiality",
    "loading_materiality_percentage_points": "loading_materiality",
    "queue_wait_timeout_seconds": "queue_wait_timeout",
    "client_response_timeout_seconds": "client_response_timeout",
    "engine_call_timeout_seconds": "engine_call_timeout",
    "startup_timeout_seconds": "startup_timeout",
    "shutdown_timeout_seconds": "shutdown_timeout",
    "fake_gateway_cardinality_entries": "fake_gateway_cardinality",
}


def load_settings(overrides: Mapping[str, object] | None = None) -> AgentSettings:
    """Load exact snake-case override keys into immutable settings."""

    if overrides is None:
        return AgentSettings()
    if not isinstance(overrides, Mapping):
        raise TypeError("overrides must be a mapping")
    if any(not isinstance(key, str) for key in overrides):
        raise TypeError("configuration keys must be strings")

    unknown = sorted(set(overrides) - set(_PARSERS))
    if unknown:
        raise ValueError(f"unknown configuration keys: {', '.join(unknown)}")

    parsed = {
        _FIELD_NAMES[key]: _PARSERS[key](overrides[key], key)
        for key in sorted(overrides)
    }
    return replace(AgentSettings(), **parsed)


def load_settings_from_environment(
    environ: Mapping[str, object] | None = None,
) -> AgentSettings:
    """Load prefixed variables, ignoring unrelated process environment entries."""

    source: Mapping[str, object] = os.environ if environ is None else environ
    if not isinstance(source, Mapping):
        raise TypeError("environ must be a mapping")

    prefixed: dict[str, object] = {}
    for key, value in source.items():
        if not isinstance(key, str):
            raise TypeError("environment keys must be strings")
        if key.startswith(ENVIRONMENT_PREFIX):
            suffix = key[len(ENVIRONMENT_PREFIX) :]
            normalized = suffix.lower()
            if normalized in prefixed:
                raise ValueError(f"duplicate configuration key: {normalized}")
            prefixed[normalized] = value
    return load_settings(prefixed)

