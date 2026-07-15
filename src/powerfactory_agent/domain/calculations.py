"""Portable immutable contracts for load-flow results and policy evaluation.

These records intentionally describe observed and derived state only.  They do
not contain vendor handles or a graph mutation operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .gateway import (
    CommandSelector,
    PrimitiveObjectSelector,
    ResultCellStatus,
    ResultVariableSelector,
)
from .models import CommandSetting, VersionedName
from .values import (
    CalculationInputDigest,
    ConfigurationKey,
    ConvergenceState,
    ExtractionRevision,
    ProductIdentity,
    Quantity,
    ViolationSeverity,
    require_aware,
    require_collection,
    require_text,
    require_uuid4,
)


MAX_CALCULATION_METRICS = 1_000


def _positive_decimal(quantity: Quantity, field_name: str) -> None:
    if quantity.value < 0:
        raise ValueError(f"{field_name} must not be negative")


class MetricKind(str, Enum):
    BUS_VOLTAGE = "bus_voltage"
    EQUIPMENT_LOADING = "equipment_loading"


class EvaluationStatus(str, Enum):
    SAFE = "safe"
    VIOLATION = "violation"
    NOT_EVALUATED_MISSING_LIMIT = "not_evaluated_missing_limit"
    NOT_EVALUATED_DATA = "not_evaluated_data"


class FindingTrend(str, Enum):
    NEW = "new"
    RESOLVED = "resolved"
    UNCHANGED = "unchanged"
    NOT_EVALUATED = "not_evaluated"


class CalculationOverlayKind(str, Enum):
    RESULT = "result"
    VIOLATION = "violation"


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    definition_id: str
    asset_identity: ProductIdentity
    metric_kind: MetricKind
    object_selector: PrimitiveObjectSelector
    variable: ResultVariableSelector
    canonical_unit: str
    lower_limit: Optional[Quantity]
    upper_limit: Optional[Quantity]
    critical_margin: Quantity
    equivalence_tolerance: Quantity
    materiality_threshold: Quantity
    limit_source: str

    def __post_init__(self) -> None:
        require_text(self.definition_id, "MetricDefinition.definition_id", maximum=256)
        require_text(self.canonical_unit, "MetricDefinition.canonical_unit", maximum=64)
        require_text(self.limit_source, "MetricDefinition.limit_source", maximum=512)
        if self.lower_limit is not None and self.lower_limit.unit != self.canonical_unit:
            raise ValueError("MetricDefinition.lower_limit unit must be canonical")
        if self.upper_limit is not None and self.upper_limit.unit != self.canonical_unit:
            raise ValueError("MetricDefinition.upper_limit unit must be canonical")
        if self.critical_margin.unit != self.canonical_unit:
            raise ValueError("MetricDefinition.critical_margin unit must be canonical")
        if self.equivalence_tolerance.unit != self.canonical_unit:
            raise ValueError("MetricDefinition.equivalence_tolerance unit must be canonical")
        if self.materiality_threshold.unit != self.canonical_unit:
            raise ValueError("MetricDefinition.materiality_threshold unit must be canonical")
        _positive_decimal(self.critical_margin, "MetricDefinition.critical_margin")
        _positive_decimal(self.equivalence_tolerance, "MetricDefinition.equivalence_tolerance")
        _positive_decimal(self.materiality_threshold, "MetricDefinition.materiality_threshold")
        if self.lower_limit is not None and self.upper_limit is not None and self.lower_limit.value > self.upper_limit.value:
            raise ValueError("MetricDefinition lower limit cannot exceed upper limit")
        if self.metric_kind is MetricKind.BUS_VOLTAGE:
            if self.variable.kind.value != "bus_voltage":
                raise ValueError("voltage metric requires the bus_voltage result selector")
        elif self.metric_kind is MetricKind.EQUIPMENT_LOADING:
            if self.variable.kind.value != "equipment_loading":
                raise ValueError("loading metric requires the equipment_loading result selector")
            if self.lower_limit is not None:
                raise ValueError("loading metrics admit an upper limit only")


@dataclass(frozen=True, slots=True)
class LoadFlowRequest:
    context_id: str
    configuration_key: ConfigurationKey
    extraction_revision: ExtractionRevision
    command: CommandSelector
    command_settings: Tuple[CommandSetting, ...]
    metric_catalog: Tuple[MetricDefinition, ...]
    policy: VersionedName
    idempotency_key: str

    def __post_init__(self) -> None:
        require_uuid4(self.context_id, "LoadFlowRequest.context_id")
        if self.extraction_revision.scope_id != self.context_id:
            raise ValueError("load-flow extraction revision must belong to context")
        require_collection(self.command_settings, "LoadFlowRequest.command_settings")
        require_collection(self.metric_catalog, "LoadFlowRequest.metric_catalog", allow_empty=False)
        if len(self.metric_catalog) > MAX_CALCULATION_METRICS:
            raise ValueError("load-flow metric catalog exceeds bound")
        names = tuple(setting.name for setting in self.command_settings)
        definitions = tuple(item.definition_id for item in self.metric_catalog)
        if len(set(names)) != len(names):
            raise ValueError("load-flow command settings must be unique")
        if definitions != tuple(sorted(definitions)) or len(set(definitions)) != len(definitions):
            raise ValueError("metric catalog requires ordered unique definition IDs")
        if len({item.asset_identity.value for item in self.metric_catalog}) != len(self.metric_catalog):
            raise ValueError("metric catalog permits one metric per asset in this policy version")
        require_text(self.idempotency_key, "LoadFlowRequest.idempotency_key", maximum=256)


@dataclass(frozen=True, slots=True)
class CalculationRun:
    run_id: str
    context_id: str
    configuration_key: ConfigurationKey
    extraction_revision: ExtractionRevision
    calculation_input_digest: CalculationInputDigest
    command: CommandSelector
    command_settings: Tuple[CommandSetting, ...]
    policy: VersionedName
    convergence_state: ConvergenceState
    execution_id: Optional[str]
    diagnostic_messages: Tuple[str, ...]
    log_references: Tuple[str, ...]
    result_snapshot_id: Optional[str]
    started_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "CalculationRun.run_id")
        require_uuid4(self.context_id, "CalculationRun.context_id")
        if self.extraction_revision.scope_id != self.context_id:
            raise ValueError("calculation run extraction revision must belong to context")
        if self.execution_id is not None:
            require_text(self.execution_id, "CalculationRun.execution_id", maximum=256)
        if self.result_snapshot_id is not None:
            require_uuid4(self.result_snapshot_id, "CalculationRun.result_snapshot_id")
        if self.convergence_state is ConvergenceState.CONVERGED and self.result_snapshot_id is None:
            raise ValueError("converged runs require an immutable result snapshot")
        if self.convergence_state is not ConvergenceState.CONVERGED and self.result_snapshot_id is not None:
            raise ValueError("non-converged and failed runs cannot claim a result snapshot")
        require_collection(self.command_settings, "CalculationRun.command_settings")
        require_collection(self.diagnostic_messages, "CalculationRun.diagnostic_messages")
        require_collection(self.log_references, "CalculationRun.log_references")
        for field_name in ("diagnostic_messages", "log_references"):
            for value in getattr(self, field_name):
                require_text(value, f"CalculationRun.{field_name} entry", maximum=1024)
        require_aware(self.started_at, "CalculationRun.started_at")
        require_aware(self.completed_at, "CalculationRun.completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("calculation run cannot complete before it starts")


@dataclass(frozen=True, slots=True)
class ResultMetric:
    definition: MetricDefinition
    status: ResultCellStatus
    source_value: Optional[str]
    source_unit: Optional[str]
    normalized: Optional[Quantity]
    diagnostic: Optional[str]

    def __post_init__(self) -> None:
        for field_name in ("source_value", "source_unit", "diagnostic"):
            value = getattr(self, field_name)
            if value is not None:
                require_text(value, f"ResultMetric.{field_name}", maximum=1024)
        if self.status is ResultCellStatus.AVAILABLE:
            if self.source_value is None or self.source_unit is None or self.normalized is None:
                raise ValueError("available metrics require source and normalized value")
            if self.normalized.unit != self.definition.canonical_unit:
                raise ValueError("available metric must use its canonical unit")
        elif self.status is ResultCellStatus.NON_FINITE:
            if self.source_value is None or self.source_unit is None or self.normalized is not None:
                raise ValueError("non-finite metrics retain source evidence only")
        elif self.source_value is not None or self.source_unit is not None or self.normalized is not None:
            raise ValueError("missing or unsupported metrics cannot have values")


@dataclass(frozen=True, slots=True)
class PolicyViolation:
    violation_key: str
    definition_id: str
    asset_identity: ProductIdentity
    metric_kind: MetricKind
    measured_value: Quantity
    limit: Quantity
    severity: ViolationSeverity
    direction: str

    def __post_init__(self) -> None:
        require_text(self.violation_key, "PolicyViolation.violation_key", maximum=512)
        require_text(self.definition_id, "PolicyViolation.definition_id", maximum=256)
        if self.direction not in {"lower", "upper"}:
            raise ValueError("violation direction must be lower or upper")
        if self.measured_value.unit != self.limit.unit:
            raise ValueError("violation measurement and limit units must match")


@dataclass(frozen=True, slots=True)
class MetricEvaluation:
    definition_id: str
    status: EvaluationStatus
    observed_status: ResultCellStatus
    violation: Optional[PolicyViolation]
    reason: Optional[str]

    def __post_init__(self) -> None:
        require_text(self.definition_id, "MetricEvaluation.definition_id", maximum=256)
        if self.reason is not None:
            require_text(self.reason, "MetricEvaluation.reason", maximum=1024)
        if self.status is EvaluationStatus.VIOLATION:
            if self.violation is None:
                raise ValueError("violation evaluation requires a violation")
        elif self.violation is not None:
            raise ValueError("only violation evaluations may include a violation")
        if self.status is EvaluationStatus.NOT_EVALUATED_MISSING_LIMIT and self.observed_status is not ResultCellStatus.AVAILABLE:
            raise ValueError("missing-limit evaluation requires available data")
        if self.status is EvaluationStatus.NOT_EVALUATED_DATA and self.observed_status is ResultCellStatus.AVAILABLE:
            raise ValueError("available data must be evaluated or report a missing limit")


@dataclass(frozen=True, slots=True)
class ResultSnapshot:
    snapshot_id: str
    run_id: str
    context_id: str
    configuration_key: ConfigurationKey
    extraction_revision: ExtractionRevision
    calculation_input_digest: CalculationInputDigest
    policy: VersionedName
    metrics: Tuple[ResultMetric, ...]
    evaluations: Tuple[MetricEvaluation, ...]
    captured_at: datetime

    def __post_init__(self) -> None:
        require_uuid4(self.snapshot_id, "ResultSnapshot.snapshot_id")
        require_uuid4(self.run_id, "ResultSnapshot.run_id")
        require_uuid4(self.context_id, "ResultSnapshot.context_id")
        if self.extraction_revision.scope_id != self.context_id:
            raise ValueError("snapshot extraction revision must belong to context")
        require_collection(self.metrics, "ResultSnapshot.metrics", allow_empty=False)
        require_collection(self.evaluations, "ResultSnapshot.evaluations")
        ids = tuple(item.definition.definition_id for item in self.metrics)
        evaluation_ids = tuple(item.definition_id for item in self.evaluations)
        if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
            raise ValueError("snapshot metrics require ordered unique definition IDs")
        if evaluation_ids != ids:
            raise ValueError("snapshot evaluations must align exactly with metrics")
        require_aware(self.captured_at, "ResultSnapshot.captured_at")


@dataclass(frozen=True, slots=True)
class MetricComparison:
    definition_id: str
    before_status: ResultCellStatus
    after_status: ResultCellStatus
    before_value: Optional[Quantity]
    after_value: Optional[Quantity]
    delta: Optional[Quantity]
    equivalent: bool
    material: bool

    def __post_init__(self) -> None:
        require_text(self.definition_id, "MetricComparison.definition_id", maximum=256)
        if (self.before_value is None) != (self.after_value is None):
            raise ValueError("metric comparisons retain both values or neither")
        if (self.before_value is None) != (self.delta is None):
            raise ValueError("metric comparisons retain a delta only with values")
        if self.before_value is not None and self.before_value.unit != self.after_value.unit:
            raise ValueError("metric comparisons require a shared unit")
        if self.delta is not None and self.before_value is not None and self.delta.unit != self.before_value.unit:
            raise ValueError("metric comparison delta requires the shared unit")
        if self.before_value is None and (self.equivalent or self.material):
            raise ValueError("non-comparable metrics cannot claim equivalent or material")
        if self.equivalent and self.material:
            raise ValueError("equivalent metrics cannot be material")


@dataclass(frozen=True, slots=True)
class FindingComparison:
    violation_key: str
    trend: FindingTrend
    baseline_status: EvaluationStatus
    candidate_status: EvaluationStatus

    def __post_init__(self) -> None:
        require_text(self.violation_key, "FindingComparison.violation_key", maximum=512)
        if self.trend is FindingTrend.NOT_EVALUATED and (
            self.baseline_status not in {EvaluationStatus.NOT_EVALUATED_DATA, EvaluationStatus.NOT_EVALUATED_MISSING_LIMIT}
            and self.candidate_status not in {EvaluationStatus.NOT_EVALUATED_DATA, EvaluationStatus.NOT_EVALUATED_MISSING_LIMIT}
        ):
            raise ValueError("not-evaluated trend requires an un-evaluated finding")


@dataclass(frozen=True, slots=True)
class CalculationComparison:
    comparison_id: str
    baseline_snapshot_id: str
    candidate_snapshot_id: str
    policy: VersionedName
    result_equivalent: bool
    comparable_metric_count: int
    comparisons: Tuple[MetricComparison, ...]
    findings: Tuple[FindingComparison, ...]
    compared_at: datetime

    def __post_init__(self) -> None:
        require_uuid4(self.comparison_id, "CalculationComparison.comparison_id")
        require_uuid4(self.baseline_snapshot_id, "CalculationComparison.baseline_snapshot_id")
        require_uuid4(self.candidate_snapshot_id, "CalculationComparison.candidate_snapshot_id")
        if self.baseline_snapshot_id == self.candidate_snapshot_id:
            raise ValueError("comparison requires two distinct snapshots")
        if isinstance(self.comparable_metric_count, bool) or not isinstance(self.comparable_metric_count, int) or self.comparable_metric_count < 0:
            raise ValueError("comparable metric count must be nonnegative")
        require_collection(self.comparisons, "CalculationComparison.comparisons")
        require_collection(self.findings, "CalculationComparison.findings")
        if self.comparable_metric_count != sum(item.before_value is not None for item in self.comparisons):
            raise ValueError("comparable metric count must match comparison values")
        require_aware(self.compared_at, "CalculationComparison.compared_at")


@dataclass(frozen=True, slots=True)
class CalculationOverlay:
    overlay_id: str
    overlay_kind: CalculationOverlayKind
    product_identity: ProductIdentity
    run_id: str
    snapshot_id: str
    policy: VersionedName
    definition_id: str
    violation_key: Optional[str]

    def __post_init__(self) -> None:
        require_text(self.overlay_id, "CalculationOverlay.overlay_id", maximum=512)
        require_uuid4(self.run_id, "CalculationOverlay.run_id")
        require_uuid4(self.snapshot_id, "CalculationOverlay.snapshot_id")
        require_text(self.definition_id, "CalculationOverlay.definition_id", maximum=256)
        if self.overlay_kind is CalculationOverlayKind.VIOLATION:
            if self.violation_key is None:
                raise ValueError("violation overlay requires a violation key")
        elif self.violation_key is not None:
            raise ValueError("result overlay cannot include a violation key")
        if self.violation_key is not None:
            require_text(self.violation_key, "CalculationOverlay.violation_key", maximum=512)


__all__ = [name for name in globals() if not name.startswith("_")]
