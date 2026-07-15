"""Immutable, unit-explicit configuration models for platform-independent code."""

from __future__ import annotations

from dataclasses import dataclass, fields
import math
from typing import ClassVar


DEVELOPMENT_DEFAULTS_PROFILE = "development-unvalidated-windows/v1"


def _require_integer(value: int, field_name: str, *, minimum: int = 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")


def _require_finite_number(value: float, field_name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be numeric")
    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        raise ValueError(f"{field_name} must be finite")
    if numeric_value <= minimum:
        raise ValueError(f"{field_name} must be greater than {minimum}")
    return numeric_value


@dataclass(frozen=True, slots=True)
class Seconds:
    """A positive duration expressed in seconds."""

    value: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _require_finite_number(self.value, "Seconds.value"))


@dataclass(frozen=True, slots=True)
class EntryCount:
    """A positive number of collection entries."""

    value: int

    def __post_init__(self) -> None:
        _require_integer(self.value, "EntryCount.value")


@dataclass(frozen=True, slots=True)
class ByteCount:
    """A positive serialized-payload size expressed in bytes."""

    value: int

    def __post_init__(self) -> None:
        _require_integer(self.value, "ByteCount.value")


@dataclass(frozen=True, slots=True)
class PerUnitDelta:
    """A positive absolute materiality delta in per-unit voltage."""

    value: float

    def __post_init__(self) -> None:
        numeric_value = _require_finite_number(self.value, "PerUnitDelta.value")
        if numeric_value > 1.0:
            raise ValueError("PerUnitDelta.value must not exceed 1.0 p.u.")
        object.__setattr__(self, "value", numeric_value)


@dataclass(frozen=True, slots=True)
class PercentagePointDelta:
    """A positive absolute materiality delta in percentage points."""

    value: float

    def __post_init__(self) -> None:
        numeric_value = _require_finite_number(self.value, "PercentagePointDelta.value")
        if numeric_value > 100.0:
            raise ValueError("PercentagePointDelta.value must not exceed 100 percentage points")
        object.__setattr__(self, "value", numeric_value)


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """Named development defaults; Windows production acceptance remains empirical."""

    DEFAULTS_PROFILE: ClassVar[str] = DEVELOPMENT_DEFAULTS_PROFILE

    cached_freshness_max_age: Seconds = Seconds(300.0)
    verified_freshness_max_age: Seconds = Seconds(30.0)
    inventory_default_page_size: EntryCount = EntryCount(100)
    inventory_max_page_size: EntryCount = EntryCount(1_000)
    maximum_collection_entries: EntryCount = EntryCount(10_000)
    maximum_serialized_payload: ByteCount = ByteCount(1_048_576)
    voltage_materiality: PerUnitDelta = PerUnitDelta(0.001)
    loading_materiality: PercentagePointDelta = PercentagePointDelta(1.0)
    queue_wait_timeout: Seconds = Seconds(30.0)
    client_response_timeout: Seconds = Seconds(120.0)
    engine_call_timeout: Seconds = Seconds(300.0)
    startup_timeout: Seconds = Seconds(60.0)
    shutdown_timeout: Seconds = Seconds(30.0)
    fake_gateway_cardinality: EntryCount = EntryCount(250)

    def __post_init__(self) -> None:
        expected_types = {
            "cached_freshness_max_age": Seconds,
            "verified_freshness_max_age": Seconds,
            "inventory_default_page_size": EntryCount,
            "inventory_max_page_size": EntryCount,
            "maximum_collection_entries": EntryCount,
            "maximum_serialized_payload": ByteCount,
            "voltage_materiality": PerUnitDelta,
            "loading_materiality": PercentagePointDelta,
            "queue_wait_timeout": Seconds,
            "client_response_timeout": Seconds,
            "engine_call_timeout": Seconds,
            "startup_timeout": Seconds,
            "shutdown_timeout": Seconds,
            "fake_gateway_cardinality": EntryCount,
        }
        for item in fields(self):
            expected = expected_types[item.name]
            if not isinstance(getattr(self, item.name), expected):
                raise TypeError(f"{item.name} must be {expected.__name__}")

        if self.verified_freshness_max_age.value > self.cached_freshness_max_age.value:
            raise ValueError("verified freshness cannot be older than cached freshness")
        if self.inventory_default_page_size.value > self.inventory_max_page_size.value:
            raise ValueError("inventory default page size cannot exceed its maximum")
        if self.inventory_max_page_size.value > self.maximum_collection_entries.value:
            raise ValueError("inventory maximum page size cannot exceed the collection limit")
        if self.fake_gateway_cardinality.value > self.maximum_collection_entries.value:
            raise ValueError("fake gateway cardinality cannot exceed the collection limit")
        if self.client_response_timeout.value > self.engine_call_timeout.value:
            raise ValueError("client response timeout cannot exceed the engine call timeout")

