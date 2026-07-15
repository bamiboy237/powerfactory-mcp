"""Owner-only portable load-flow application service and policy evaluation."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import time
import uuid

from powerfactory_agent.domain import (
    CalculationComparison,
    CalculationRun,
    CommandCompletion,
    CommandExecutionObservation,
    CommandExecutionRequest,
    EvaluationStatus,
    FindingComparison,
    FindingTrend,
    LoadFlowRequest,
    LogBatch,
    LogReadRequest,
    MetricComparison,
    MetricDefinition,
    MetricEvaluation,
    PolicyViolation,
    ResultBatch,
    ResultCellStatus,
    ResultCollectionRequest,
    ResultMetric,
    ResultSnapshot,
    Quantity,
    ViolationSeverity,
    ConvergenceState,
)
from powerfactory_agent.gateway import (
    OperationResultUnavailableError,
    SerializedPowerFactoryOwner,
)
from powerfactory_agent.persistence import CalculationStore, OperationState
from powerfactory_agent.serialization import canonical_digest


class CalculationServiceError(RuntimeError):
    pass


class CalculationOperationFailed(CalculationServiceError):
    pass


class CalculationOperationTimedOut(CalculationServiceError):
    pass


class CalculationPaginationError(CalculationServiceError):
    pass


class LoadFlowService:
    """Buildout 5 runs through ``SerializedPowerFactoryOwner`` exclusively."""

    def __init__(
        self,
        owner: SerializedPowerFactoryOwner,
        store: CalculationStore,
        *,
        result_page_size: int = 100,
        log_entry_limit: int = 32,
        log_byte_limit: int = 16_384,
        owner_wait_timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(owner, SerializedPowerFactoryOwner):
            raise TypeError("load-flow service requires SerializedPowerFactoryOwner")
        if not 1 <= result_page_size <= 100:
            raise ValueError("result_page_size must be between 1 and 100")
        if not 1 <= log_entry_limit <= 100 or not 1 <= log_byte_limit <= 65_536:
            raise ValueError("log bounds are invalid")
        if owner_wait_timeout_seconds <= 0:
            raise ValueError("owner wait timeout must be positive")
        self._owner = owner
        self._store = store
        self._result_page_size = result_page_size
        self._log_entry_limit = log_entry_limit
        self._log_byte_limit = log_byte_limit
        self._owner_wait_timeout_seconds = owner_wait_timeout_seconds

    def run_validated_load_flow(self, request: LoadFlowRequest) -> CalculationRun:
        """Execute, capture, evaluate, and durably store one immutable load-flow run."""
        started_at = _utc_now()
        digest = CalculationRunInput.digest(request)
        run_id = str(uuid.uuid4())
        try:
            execution = self._await(
                self._owner.submit_execute_command(
                    CommandExecutionRequest(
                        request.configuration_key,
                        request.command,
                        request.command_settings,
                        request.idempotency_key,
                    ),
                    idempotency_key=request.idempotency_key,
                ),
                CommandExecutionObservation,
            )
        except CalculationServiceError as exc:
            run = self._failed_run(request, run_id, digest, started_at, str(exc))
            return self._store.record(run)

        log_references, log_diagnostics = self._logs(execution.execution_id, request.idempotency_key)
        if execution.completion is not CommandCompletion.SUCCEEDED:
            run = CalculationRun(
                run_id,
                request.context_id,
                request.configuration_key,
                request.extraction_revision,
                digest,
                request.command,
                request.command_settings,
                request.policy,
                ConvergenceState.NOT_CONVERGED,
                execution.execution_id,
                _bounded_messages(execution.diagnostic_messages + log_diagnostics),
                log_references,
                None,
                started_at,
                _utc_now(),
            )
            return self._store.record(run)

        try:
            metrics = self._collect_metrics(request, execution.execution_id)
        except CalculationServiceError as exc:
            run = self._failed_run(
                request,
                run_id,
                digest,
                started_at,
                str(exc),
                execution_id=execution.execution_id,
                log_references=log_references,
            )
            return self._store.record(run)
        evaluations = tuple(evaluate_metric(metric) for metric in metrics)
        snapshot_id = str(uuid.uuid4())
        snapshot = ResultSnapshot(
            snapshot_id,
            run_id,
            request.context_id,
            request.configuration_key,
            request.extraction_revision,
            digest,
            request.policy,
            metrics,
            evaluations,
            _utc_now(),
        )
        run = CalculationRun(
            run_id,
            request.context_id,
            request.configuration_key,
            request.extraction_revision,
            digest,
            request.command,
            request.command_settings,
            request.policy,
            ConvergenceState.CONVERGED,
            execution.execution_id,
            _bounded_messages(execution.diagnostic_messages + log_diagnostics),
            log_references,
            snapshot_id,
            started_at,
            _utc_now(),
        )
        return self._store.record(run, snapshot)

    def compare_snapshots(self, baseline_snapshot_id: str, candidate_snapshot_id: str) -> CalculationComparison:
        baseline = self._store.snapshot(baseline_snapshot_id)
        candidate = self._store.snapshot(candidate_snapshot_id)
        comparison = compare_result_snapshots(baseline, candidate)
        return self._store.record_comparison(comparison)

    def get_calculation_run(self, run_id: str) -> CalculationRun:
        """Retrieve immutable calculation evidence without touching the gateway."""
        return self._store.run(run_id)

    def find_violations(self, snapshot_id: str) -> tuple[PolicyViolation, ...]:
        """Return policy-derived violations from one immutable result snapshot."""
        snapshot = self._store.snapshot(snapshot_id)
        return tuple(
            evaluation.violation
            for evaluation in snapshot.evaluations
            if evaluation.violation is not None
        )

    def _collect_metrics(self, request: LoadFlowRequest, execution_id: str) -> tuple[ResultMetric, ...]:
        objects = tuple(item.object_selector for item in request.metric_catalog)
        variables = tuple(dict.fromkeys(item.variable for item in request.metric_catalog))
        results: dict[object, dict[object, object]] = {}
        cursor = None
        seen_cursors: set[str] = set()
        while True:
            batch = self._await(
                self._owner.submit_collect_results(
                    ResultCollectionRequest(
                        request.configuration_key,
                        execution_id,
                        objects,
                        variables,
                        self._result_page_size,
                        cursor,
                    ),
                    idempotency_key=f"{request.idempotency_key}:results:{len(seen_cursors)}",
                ),
                ResultBatch,
            )
            if batch.execution_id != execution_id:
                raise CalculationServiceError("owner returned results for another execution")
            for row in batch.rows:
                if row.selector in results:
                    raise CalculationPaginationError("result collection repeated an object selector")
                results[row.selector] = {cell.variable: cell for cell in row.cells}
            if batch.complete:
                break
            if batch.next_cursor is None or batch.next_cursor.token in seen_cursors:
                raise CalculationPaginationError("result collection cursor did not make progress")
            seen_cursors.add(batch.next_cursor.token)
            cursor = batch.next_cursor
            if len(seen_cursors) > len(objects):
                raise CalculationPaginationError("result collection exceeded bounded object pages")
        metrics: list[ResultMetric] = []
        for definition in request.metric_catalog:
            cell = results.get(definition.object_selector, {}).get(definition.variable)
            if cell is None:
                metrics.append(ResultMetric(definition, ResultCellStatus.MISSING, None, None, None, "result cell was absent"))
                continue
            metrics.append(
                ResultMetric(
                    definition,
                    cell.status,
                    cell.source_value,
                    cell.source_unit,
                    cell.normalized,
                    cell.diagnostic,
                )
            )
        return tuple(metrics)

    def _logs(self, execution_id: str, key: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        try:
            batch = self._await(
                self._owner.submit_read_logs(
                    LogReadRequest(execution_id, self._log_entry_limit, self._log_byte_limit, None),
                    idempotency_key=f"{key}:logs",
                ),
                LogBatch,
            )
        except CalculationServiceError as exc:
            return (), (f"log collection unavailable: {exc}",)
        references = tuple(f"execution:{execution_id}:log:{item.sequence}" for item in batch.entries)
        diagnostics = tuple(item.message for item in batch.entries if item.severity.value in {"warning", "error"})
        return references, _bounded_messages(diagnostics)

    def _await(self, record: object, result_type: type[object]) -> object:
        operation_id = getattr(record, "operation_id", None)
        if not isinstance(operation_id, str):
            raise CalculationServiceError("owner did not return a durable operation record")
        deadline = time.monotonic() + self._owner_wait_timeout_seconds
        while time.monotonic() < deadline:
            status = self._owner.status(operation_id)
            if status.terminal:
                try:
                    return self._owner.completed_result(operation_id, result_type)
                except OperationResultUnavailableError as exc:
                    raise CalculationOperationFailed(f"owner operation ended as {status.state.value}") from exc
            if status.state in {OperationState.ENGINE_UNRESPONSIVE, OperationState.RECONCILIATION_REQUIRED}:
                raise CalculationOperationFailed(f"owner operation requires reconciliation: {status.state.value}")
            time.sleep(0.002)
        raise CalculationOperationTimedOut("owner operation did not become terminal before service timeout")

    @staticmethod
    def _failed_run(
        request: LoadFlowRequest,
        run_id: str,
        digest: object,
        started_at: datetime,
        diagnostic: str,
        *,
        execution_id: str | None = None,
        log_references: tuple[str, ...] = (),
    ) -> CalculationRun:
        assert hasattr(digest, "value")
        return CalculationRun(
            run_id,
            request.context_id,
            request.configuration_key,
            request.extraction_revision,
            digest,
            request.command,
            request.command_settings,
            request.policy,
            ConvergenceState.FAILED,
            execution_id,
            _bounded_messages((diagnostic,)),
            log_references,
            None,
            started_at,
            _utc_now(),
        )


class CalculationRunInput:
    @staticmethod
    def digest(request: LoadFlowRequest):
        from powerfactory_agent.domain import CalculationInputDigest

        return CalculationInputDigest(
            canonical_digest(
                {
                    "configuration_key": request.configuration_key,
                    "extraction_revision": request.extraction_revision,
                    "command": request.command,
                    "command_settings": request.command_settings,
                    "metric_catalog": request.metric_catalog,
                    "policy": request.policy,
                },
                kind="calculation-input",
            )
        )


def evaluate_metric(metric: ResultMetric) -> MetricEvaluation:
    definition = metric.definition
    if metric.status is not ResultCellStatus.AVAILABLE or metric.normalized is None:
        return MetricEvaluation(
            definition.definition_id,
            EvaluationStatus.NOT_EVALUATED_DATA,
            metric.status,
            None,
            metric.diagnostic or "result data is unavailable, unsupported, or non-finite",
        )
    if definition.lower_limit is None and definition.upper_limit is None:
        return MetricEvaluation(
            definition.definition_id,
            EvaluationStatus.NOT_EVALUATED_MISSING_LIMIT,
            metric.status,
            None,
            "no admitted engineering limit is available",
        )
    if definition.lower_limit is not None and metric.normalized.value < definition.lower_limit.value:
        return _violation_evaluation(metric, definition.lower_limit, "lower")
    if definition.upper_limit is not None and metric.normalized.value > definition.upper_limit.value:
        return _violation_evaluation(metric, definition.upper_limit, "upper")
    return MetricEvaluation(definition.definition_id, EvaluationStatus.SAFE, metric.status, None, None)


def compare_result_snapshots(baseline: ResultSnapshot, candidate: ResultSnapshot) -> CalculationComparison:
    if baseline.configuration_key != candidate.configuration_key:
        raise CalculationServiceError("snapshot comparison requires matching configuration keys")
    if baseline.policy != candidate.policy:
        raise CalculationServiceError("snapshot comparison requires matching policy versions")
    before = {item.definition.definition_id: item for item in baseline.metrics}
    after = {item.definition.definition_id: item for item in candidate.metrics}
    if set(before) != set(after):
        raise CalculationServiceError("snapshot metric catalogs do not align")
    comparisons: list[MetricComparison] = []
    findings: list[FindingComparison] = []
    before_evaluations = {item.definition_id: item for item in baseline.evaluations}
    after_evaluations = {item.definition_id: item for item in candidate.evaluations}
    for definition_id in sorted(before):
        prior, current = before[definition_id], after[definition_id]
        comparable = prior.status is ResultCellStatus.AVAILABLE and current.status is ResultCellStatus.AVAILABLE
        before_value = prior.normalized if comparable else None
        after_value = current.normalized if comparable else None
        if comparable:
            assert before_value is not None and after_value is not None
            delta = after_value.value - before_value.value
            equivalent = abs(delta) <= current.definition.equivalence_tolerance.value
            material = abs(delta) > current.definition.materiality_threshold.value
            delta_quantity = Quantity(delta, after_value.unit)
        else:
            equivalent = False
            material = False
            delta_quantity = None
        comparisons.append(
            MetricComparison(
                definition_id,
                prior.status,
                current.status,
                before_value,
                after_value,
                delta_quantity,
                equivalent,
                material,
            )
        )
        findings.append(
            _compare_finding(before_evaluations[definition_id], after_evaluations[definition_id], prior.definition)
        )
    comparable_count = sum(item.before_value is not None for item in comparisons)
    return CalculationComparison(
        str(uuid.uuid4()),
        baseline.snapshot_id,
        candidate.snapshot_id,
        baseline.policy,
        comparable_count > 0 and all(item.equivalent for item in comparisons if item.before_value is not None),
        comparable_count,
        tuple(comparisons),
        tuple(findings),
        _utc_now(),
    )


def _violation_evaluation(metric: ResultMetric, limit, direction: str) -> MetricEvaluation:
    assert metric.normalized is not None
    delta = (
        limit.value - metric.normalized.value if direction == "lower" else metric.normalized.value - limit.value
    )
    severity = ViolationSeverity.CRITICAL if delta > metric.definition.critical_margin.value else ViolationSeverity.WARNING
    violation = PolicyViolation(
        f"{metric.definition.asset_identity.value}:{metric.definition.metric_kind.value}:{direction}",
        metric.definition.definition_id,
        metric.definition.asset_identity,
        metric.definition.metric_kind,
        metric.normalized,
        limit,
        severity,
        direction,
    )
    return MetricEvaluation(metric.definition.definition_id, EvaluationStatus.VIOLATION, metric.status, violation, None)


def _compare_finding(before: MetricEvaluation, after: MetricEvaluation, definition: MetricDefinition) -> FindingComparison:
    before_violation = before.violation is not None
    after_violation = after.violation is not None
    key = f"{definition.asset_identity.value}:{definition.metric_kind.value}:"
    if before.status in {EvaluationStatus.NOT_EVALUATED_DATA, EvaluationStatus.NOT_EVALUATED_MISSING_LIMIT} or after.status in {
        EvaluationStatus.NOT_EVALUATED_DATA,
        EvaluationStatus.NOT_EVALUATED_MISSING_LIMIT,
    }:
        trend = FindingTrend.NOT_EVALUATED
    elif not before_violation and after_violation:
        trend = FindingTrend.NEW
    elif before_violation and not after_violation:
        trend = FindingTrend.RESOLVED
    else:
        trend = FindingTrend.UNCHANGED
    actual_key = (after.violation or before.violation)
    return FindingComparison(
        actual_key.violation_key if actual_key is not None else key + "evaluated",
        trend,
        before.status,
        after.status,
    )


def _bounded_messages(messages: tuple[str, ...], *, limit: int = 16) -> tuple[str, ...]:
    return tuple(message[:1024] for message in messages[:limit])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "CalculationOperationFailed",
    "CalculationOperationTimedOut",
    "CalculationPaginationError",
    "CalculationRunInput",
    "CalculationServiceError",
    "LoadFlowService",
    "compare_result_snapshots",
    "evaluate_metric",
]
