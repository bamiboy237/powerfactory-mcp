"""Deterministic headline-workflow harness for portable product tests."""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
import hashlib
import hmac
import json
from threading import RLock
from typing import Any, Callable, Tuple

from powerfactory_agent.domain import (
    AppliedChange,
    AssetKind,
    AssetReference,
    AttributeQuantity,
    CalculationInputDigest,
    ChangePreview,
    CommandSetting,
    CompletenessState,
    ConfigurationKey,
    ConfirmedAssetChange,
    ContentDigest,
    ConvergenceChange,
    ConvergenceState,
    DependencyFingerprint,
    DependencySetIdentity,
    ExecutionAuthorization,
    ExtractionRevision,
    FreshnessEvidence,
    FreshnessLevel,
    LiveStateFingerprint,
    LoadFlowRun,
    IdentityLifecycleState,
    InventoryPage,
    InventoryQuery,
    LocatorEvidenceSchema,
    LocatorKind,
    LocatorTrust,
    MetricDelta,
    ModelContext,
    MutationStrategy,
    OperationType,
    PageCursor,
    PowerFactoryLocator,
    ProjectProvenance,
    ProductIdentity,
    Quantity,
    RestorationValue,
    ResultComparison,
    RollbackConflict,
    RollbackPlan,
    RollbackResult,
    VerificationEvidence,
    VerificationStatus,
    VersionedName,
    Violation,
    ViolationSeverity,
    ViolationTrend,
    WorkflowVersion,
    WorkspaceDisposition,
    WorkspaceRevision,
)
from powerfactory_agent.serialization import canonical_digest

from .errors import (
    AuthorizationInvalid,
    AuthorizationRequired,
    CalculationNonConvergence,
    ConfigurationMismatch,
    CursorInvalid,
    CursorStale,
    InvalidOperation,
    ObjectNotFound,
    PartialMutation,
    RollbackConflictError,
    StaleContext,
)


AttributeRead = Tuple[AssetReference, Tuple[AttributeQuantity, ...]]
ResultRead = Tuple[AssetReference, Tuple[AttributeQuantity, ...]]


_FIXED_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
_SESSION_ID = "fake-session-2026-07-14"
_MODEL_CONTEXT_ID = "10000000-0000-4000-8000-000000000001"
_WORKSPACE_ID = "20000000-0000-4000-8000-000000000001"
_CONFIGURATION = ConfigurationKey(
    canonical_digest(
        {"adapter": "fake-2026", "profile": "fixture", "project": "project-fixture", "study": "base"},
        kind="configuration-key",
    )
)
_POLICY = VersionedName("fake-engineering-policy", "1.0.0")
_PROJECT_PROVENANCE = ProjectProvenance(
    installation_id="fake-installation",
    profile_id="fake-profile",
    project_key="project-fixture",
    project_evidence="deterministic fixture project",
)
_LOCATOR_SCHEMA = LocatorEvidenceSchema("powerfactory-locator", "v1", "fake-gateway/0.1.0")
_FAKE_GATEWAY_VERSION = "fake-gateway/0.2.0"
_RESULT_SCHEMA = "fake-result-snapshot/v1"
_CURSOR_SCHEMA = "fake-inventory-cursor/v1"
_DEFAULT_CURSOR_SECRET = b"powerfactory-agent-fake-cursor-secret-v1"
_CURSOR_TTL = timedelta(minutes=5)
_MAX_READ_CELLS = 100


@dataclass(frozen=True, slots=True)
class _ResultSnapshot:
    snapshot_id: str
    run_id: str
    values: Tuple[ResultRead, ...]


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def wrapped(self: "DeterministicHeadlineHarness", *args: object, **kwargs: object) -> object:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _quantity(value: Decimal | int | str, unit: str) -> Quantity:
    return Quantity(_decimal(value), unit)


_DEFAULT_COMMAND_SETTINGS = (
    CommandSetting("calculate_voltage_drops", True),
    CommandSetting("network_representation", "balanced"),
    CommandSetting("solver_tolerance", _quantity("0.0001", "p.u.")),
)


def _asset(
    uuid: str,
    display_name: str,
    asset_kind: AssetKind,
    object_class: str,
) -> AssetReference:
    canonical_path = f"Grid/{display_name}.{object_class}"
    return AssetReference(
        product_identity=ProductIdentity(uuid),
        locator=PowerFactoryLocator(
            locator_version_id=uuid,
            locator_kind=LocatorKind.CANONICAL_PATH_FALLBACK,
            project_provenance=_PROJECT_PROVENANCE,
            object_class=object_class,
            native_field=None,
            native_value=None,
            canonical_path=canonical_path,
            evidence_schema=_LOCATOR_SCHEMA,
            observed_at=_FIXED_NOW,
            session_id=_SESSION_ID,
            trust=LocatorTrust.FALLBACK,
        ),
        display_name=display_name,
        asset_kind=asset_kind,
        project_key="project-fixture",
        lifecycle_state=IdentityLifecycleState.ACTIVE,
    )


class DeterministicHeadlineHarness:
    """Workflow test harness, not the vendor gateway or a durable authority."""

    max_page_size = 100

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        cursor_secret: bytes | None = None,
    ) -> None:
        self._lock = RLock()
        self._clock = clock or (lambda: _FIXED_NOW)
        self._cursor_secret = cursor_secret or _DEFAULT_CURSOR_SECRET
        if not isinstance(self._cursor_secret, bytes) or len(self._cursor_secret) < 32:
            raise ValueError("cursor_secret must contain at least 32 bytes")
        self._configuration = _CONFIGURATION
        self._workspace_revision = 0
        self._workflow_counters: dict[str, int] = {}
        self._run_sequence = 0
        self._change_sequence = 0
        self._registered_authorizations: dict[str, ExecutionAuthorization] = {}
        self._execution_states: dict[str, str] = {}
        self._partial_mutation_after: int | None = None

        self.area = _asset("00000000-0000-4000-8000-000000000001", "North", AssetKind.AREA, "ElmArea")
        self.bus_1 = _asset("00000000-0000-4000-8000-000000000002", "North Bus 1", AssetKind.BUS, "ElmTerm")
        self.bus_2 = _asset("00000000-0000-4000-8000-000000000003", "North Bus 2", AssetKind.BUS, "ElmTerm")
        self.line = _asset("00000000-0000-4000-8000-000000000004", "North Tie", AssetKind.LINE, "ElmLne")
        self.load_1 = _asset("00000000-0000-4000-8000-000000000005", "North Load 1", AssetKind.LOAD, "ElmLod")
        self.load_2 = _asset("00000000-0000-4000-8000-000000000006", "North Load 2", AssetKind.LOAD, "ElmLod")
        self._assets = tuple(
            sorted(
                (self.area, self.bus_1, self.bus_2, self.line, self.load_1, self.load_2),
                key=lambda item: item.product_identity.value,
            )
        )
        self._by_id = {item.product_identity.value: item for item in self._assets}
        self._area_members = {
            self.area.product_identity.value: (
                self.bus_1.product_identity.value,
                self.bus_2.product_identity.value,
                self.line.product_identity.value,
                self.load_1.product_identity.value,
                self.load_2.product_identity.value,
            )
        }
        self._values: dict[str, dict[str, Quantity]] = {
            self.area.product_identity.value: {},
            self.bus_1.product_identity.value: {"nominal_voltage": _quantity(110, "kV")},
            self.bus_2.product_identity.value: {"nominal_voltage": _quantity(110, "kV")},
            self.line.product_identity.value: {"rating": _quantity(50, "MVA")},
            self.load_1.product_identity.value: {
                "active_power": _quantity(10, "MW"),
                "reactive_power": _quantity(3, "Mvar"),
            },
            self.load_2.product_identity.value: {
                "active_power": _quantity(20, "MW"),
                "reactive_power": _quantity(6, "Mvar"),
            },
        }
        self._runs: dict[str, LoadFlowRun] = {}
        self._snapshots: dict[str, _ResultSnapshot] = {}
        self._applied: dict[str, AppliedChange] = {}
        self._rollback_results: dict[str, RollbackResult] = {}

    @_locked
    def active_context(self) -> ModelContext:
        dependency_set = DependencySetIdentity("headline-loads", "v1")
        fingerprint = self.live_state_fingerprint(
            self._configuration,
            (self.load_1, self.load_2),
            ("active_power", "reactive_power"),
        )
        return ModelContext(
            model_context_id=_MODEL_CONTEXT_ID,
            configuration_key=self._configuration,
            powerfactory_version="fake-2026",
            assets=self._assets,
            extraction_revision=ExtractionRevision(_MODEL_CONTEXT_ID, 1),
            extracted_at=self._now(),
            freshness=FreshnessEvidence(
                level=FreshnessLevel.LIVE,
                observed_at=self._now(),
                session_id=_SESSION_ID,
                configuration_key=self._configuration,
                dependency_set=dependency_set,
                evidence_reference="fake:active-context",
                policy_name="fake-live-operation",
                policy_version="v1",
                operation_active=True,
            ),
            dependency_fingerprints=(
                DependencyFingerprint(
                    dependency_set=dependency_set,
                    fingerprint=fingerprint,
                    completeness=CompletenessState.COMPLETE,
                    observed_at=self._now(),
                    session_id=_SESSION_ID,
                    evidence_reference="fake:headline-loads",
                    policy=VersionedName("fake-fingerprint-policy", "v1"),
                ),
            ),
        )

    @_locked
    def inventory(self, query: InventoryQuery) -> InventoryPage:
        self._require_configuration(query.configuration_key)
        if query.extraction_revision != ExtractionRevision(_MODEL_CONTEXT_ID, 1):
            raise InvalidOperation("inventory query is bound to another extraction revision")
        selected = tuple(
            item
            for item in self._assets
            if (query.asset_kind is None or item.asset_kind is query.asset_kind)
            and (query.exact_project_key is None or item.project_key == query.exact_project_key)
        )
        binding = canonical_digest(
            {
                "asset_kind": query.asset_kind.value if query.asset_kind else None,
                "configuration_key": query.configuration_key.value,
                "exact_project_key": query.exact_project_key,
                "extraction_revision": query.extraction_revision.wire,
                "sort": query.sort_specification,
            }
        )
        offset = 0
        if query.cursor is not None:
            cursor = self._decode_cursor(query.cursor)
            expected = {
                "schema": _CURSOR_SCHEMA,
                "query_digest": binding,
                "configuration_key": query.configuration_key.value,
                "extraction_revision": query.extraction_revision.wire,
                "sort": query.sort_specification,
                "requested_page_size": query.page_size,
            }
            if any(cursor[name] != value for name, value in expected.items()):
                raise CursorInvalid("inventory cursor is bound to another query")
            offset = cursor["offset"]
            if not isinstance(offset, int) or isinstance(offset, bool):
                raise CursorInvalid("inventory cursor offset is invalid")
            if offset < 0 or offset > len(selected):
                raise CursorInvalid("inventory cursor is outside the result snapshot")
            expected_last = selected[offset - 1].product_identity.value if offset else None
            if cursor["last_product_uuid"] != expected_last:
                raise CursorInvalid("inventory cursor position does not match the result snapshot")
        page = selected[offset : offset + query.page_size]
        next_offset = offset + len(page)
        next_cursor = None
        if next_offset < len(selected):
            issued_at = self._now()
            next_cursor = self._encode_cursor(
                {
                    "schema": _CURSOR_SCHEMA,
                    "query_digest": binding,
                    "configuration_key": query.configuration_key.value,
                    "extraction_revision": query.extraction_revision.wire,
                    "sort": query.sort_specification,
                    "last_product_uuid": page[-1].product_identity.value,
                    "offset": next_offset,
                    "requested_page_size": query.page_size,
                    "effective_page_size": len(page),
                    "issued_at": int(issued_at.timestamp()),
                    "expires_at": int((issued_at + _CURSOR_TTL).timestamp()),
                }
            )
        return InventoryPage(query=query, items=page, next_cursor=next_cursor)

    @_locked
    def area_assets(
        self,
        configuration_key: ConfigurationKey,
        area: AssetReference,
        *,
        asset_kind: AssetKind,
        limit: int,
    ) -> Tuple[AssetReference, ...]:
        self._require_configuration(configuration_key)
        self._require_limit(limit)
        resolved = self._resolve(area)
        if resolved.asset_kind is not AssetKind.AREA:
            raise InvalidOperation("area_assets requires an area reference")
        member_ids = self._area_members.get(resolved.product_identity.value)
        if member_ids is None:
            raise ObjectNotFound("area membership is unavailable")
        selected = tuple(
            asset for asset in (self._by_id[item] for item in member_ids) if asset.asset_kind == asset_kind
        )
        if len(selected) > limit:
            raise InvalidOperation("area result exceeds the requested bound")
        return selected

    @_locked
    def read_attributes(
        self,
        configuration_key: ConfigurationKey,
        assets: Tuple[AssetReference, ...],
        attributes: Tuple[str, ...],
        *,
        limit: int,
    ) -> Tuple[AttributeRead, ...]:
        self._require_configuration(configuration_key)
        self._require_limit(limit)
        if not assets or not attributes:
            raise InvalidOperation("assets and attributes must not be empty")
        if len(assets) * len(attributes) > limit:
            raise InvalidOperation("attribute read exceeds the requested bound")
        reads: list[AttributeRead] = []
        for requested in sorted(assets, key=lambda item: item.product_identity.value):
            asset = self._resolve(requested)
            values = self._values[asset.product_identity.value]
            quantities: list[AttributeQuantity] = []
            for attribute in sorted(attributes):
                if attribute not in values:
                    raise InvalidOperation(
                        f"attribute {attribute!r} is not available for {asset.asset_kind}"
                    )
                quantities.append(AttributeQuantity(attribute, values[attribute]))
            reads.append((asset, tuple(quantities)))
        return tuple(reads)

    @_locked
    def live_state_fingerprint(
        self,
        configuration_key: ConfigurationKey,
        assets: Tuple[AssetReference, ...],
        attributes: Tuple[str, ...],
    ) -> LiveStateFingerprint:
        reads = self.read_attributes(
            configuration_key,
            assets,
            attributes,
            limit=_MAX_READ_CELLS,
        )
        payload = (
            configuration_key.value,
            tuple(
                (
                    asset.product_identity.value,
                    tuple((item.attribute, str(item.value.value), item.value.unit) for item in values),
                )
                for asset, values in reads
            ),
        )
        return LiveStateFingerprint(canonical_digest(payload, kind="live-state-fingerprint"))

    @_locked
    def apply_change(
        self,
        preview: ChangePreview,
        authorization: ExecutionAuthorization,
    ) -> AppliedChange:
        if authorization is None:
            raise AuthorizationRequired("an execution authorization is required")
        self._require_registered_authorization(authorization)
        self._require_configuration(preview.configuration_key)
        if preview.expires_at <= self._now():
            raise StaleContext("change preview has expired")
        if preview.workspace_id != _WORKSPACE_ID:
            raise StaleContext("change preview is bound to another workspace")
        if preview.workspace_revision != WorkspaceRevision(_WORKSPACE_ID, self._workspace_revision):
            raise StaleContext("change preview workspace revision is stale")
        current_workflow = self._workflow_counters.get(preview.workflow_id)
        if current_workflow is None or preview.expected_workflow_version.counter != current_workflow:
            raise StaleContext("change preview workflow version is stale")
        assets = tuple(change.asset for change in preview.resolved_changes)
        attributes = tuple(sorted({item.attribute for change in preview.resolved_changes for item in change.before}))
        current_fingerprint = self.live_state_fingerprint(preview.configuration_key, assets, attributes)
        if current_fingerprint != preview.live_state_fingerprint:
            raise StaleContext("live dependency values changed after preview")

        for proposed_change in preview.resolved_changes:
            asset = self._resolve(proposed_change.asset)
            live = self._values[asset.product_identity.value]
            for before in proposed_change.before:
                if live.get(before.attribute) != before.value:
                    raise StaleContext("a target value no longer matches the preview")
        self._reserve_authorization(
            authorization,
            content_digest=preview.content_digest,
            configuration_key=preview.configuration_key,
            live_state_fingerprint=preview.live_state_fingerprint,
            operation_type=preview.operation.operation_type,
            expected_workflow_version=preview.expected_workflow_version,
        )

        self._change_sequence += 1
        applied_change_id = f"change-{self._change_sequence:04d}"
        confirmed: list[ConfirmedAssetChange] = []
        write_count = 0
        for proposed_change in preview.resolved_changes:
            asset = self._resolve(proposed_change.asset)
            live = self._values[asset.product_identity.value]
            before_by_name = {item.attribute: item.value for item in proposed_change.before}
            proposed_by_name = {item.attribute: item.value for item in proposed_change.proposed}
            for attribute in sorted(before_by_name):
                if live.get(attribute) != before_by_name[attribute]:
                    raise StaleContext("a target value no longer matches the preview")
                live[attribute] = proposed_by_name[attribute]
                confirmed.append(
                    ConfirmedAssetChange(
                        asset=asset,
                        before=(AttributeQuantity(attribute, before_by_name[attribute]),),
                        proposed=(AttributeQuantity(attribute, proposed_by_name[attribute]),),
                        confirmed=(AttributeQuantity(attribute, live[attribute]),),
                    )
                )
                write_count += 1
                if self._partial_mutation_after is not None and write_count >= self._partial_mutation_after:
                    self._partial_mutation_after = None
                    partial = self._record_applied(
                        applied_change_id,
                        authorization,
                        preview.content_digest,
                        tuple(confirmed),
                        VerificationStatus.RECONCILIATION_REQUIRED,
                    )
                    raise PartialMutation(
                        "fault injection interrupted the mutation after confirmed writes",
                        applied_change=partial,
                    )

        applied = self._record_applied(
            applied_change_id,
            authorization,
            preview.content_digest,
            tuple(confirmed),
            VerificationStatus.VERIFIED,
        )
        return applied

    @_locked
    def run_load_flow(
        self,
        configuration_key: ConfigurationKey,
        command_settings: Tuple[CommandSetting, ...],
    ) -> LoadFlowRun:
        self._require_configuration(configuration_key)
        normalized_settings = self._normalize_command_settings(command_settings)
        total_active = sum(
            (_decimal(self._values[load.product_identity.value]["active_power"].value) for load in (self.load_1, self.load_2)),
            Decimal(0),
        )
        if total_active > Decimal("90"):
            raise CalculationNonConvergence(
                "deterministic fake does not converge above 90 MW",
                details={"total_active_power": f"{total_active} MW"},
            )
        self._run_sequence += 1
        run_id = f"run-{self._run_sequence:04d}"
        snapshot_id = f"snapshot-{self._run_sequence:04d}"
        bus_1_voltage = Decimal("1.000") - total_active * Decimal("0.001")
        bus_2_voltage = Decimal("1.010") - total_active * Decimal("0.0015")
        line_loading = total_active / Decimal("50") * Decimal("100")
        values: Tuple[ResultRead, ...] = (
            (self.bus_1, (AttributeQuantity("voltage", _quantity(bus_1_voltage, "p.u.")),)),
            (self.bus_2, (AttributeQuantity("voltage", _quantity(bus_2_voltage, "p.u.")),)),
            (self.line, (AttributeQuantity("loading", _quantity(line_loading, "%")),)),
        )
        calculation_digest = CalculationInputDigest(
            canonical_digest(
                self._calculation_input_payload(configuration_key, normalized_settings),
                kind="calculation-input",
            )
        )
        run = LoadFlowRun(
            run_id=run_id,
            configuration_key=configuration_key,
            calculation_input_digest=calculation_digest,
            command_settings=normalized_settings,
            engineering_policy=_POLICY,
            convergence_state=ConvergenceState.CONVERGED,
            diagnostic_messages=(),
            result_snapshot_id=snapshot_id,
            duration=_quantity("0.2", "s"),
            log_references=(f"fake-logs/{run_id}",),
            started_at=self._now(),
            completed_at=self._now() + timedelta(milliseconds=200),
        )
        self._runs[run_id] = run
        self._snapshots[run_id] = _ResultSnapshot(snapshot_id, run_id, values)
        return run

    @_locked
    def result_snapshot(self, run_id: str, *, limit: int) -> Tuple[ResultRead, ...]:
        self._require_limit(limit)
        snapshot = self._snapshots.get(run_id)
        if snapshot is None:
            raise ObjectNotFound("load-flow result snapshot was not found")
        if len(snapshot.values) > limit:
            raise InvalidOperation("result snapshot exceeds the requested bound")
        return snapshot.values

    @_locked
    def collect_violations(self, run_id: str, *, limit: int) -> Tuple[Violation, ...]:
        self._require_limit(limit)
        values = self.result_snapshot(run_id, limit=self.max_page_size)
        violations: list[Violation] = []
        for asset, metrics in values:
            for metric in metrics:
                value = _decimal(metric.value.value)
                if metric.attribute == "voltage" and value < Decimal("0.95"):
                    engineering_limit = _quantity("0.95", "p.u.")
                    violation_type = "undervoltage"
                    severity = ViolationSeverity.CRITICAL if value < Decimal("0.90") else ViolationSeverity.WARNING
                elif metric.attribute == "loading" and value > Decimal("100"):
                    engineering_limit = _quantity("100", "%")
                    violation_type = "thermal_overload"
                    severity = ViolationSeverity.CRITICAL if value > Decimal("120") else ViolationSeverity.WARNING
                else:
                    continue
                violations.append(
                    Violation(
                        violation_id=f"violation:{asset.product_identity.value}:{violation_type}",
                        asset=asset,
                        violation_type=violation_type,
                        measured_value=metric.value,
                        limit=engineering_limit,
                        severity=severity,
                        source_calculation_id=run_id,
                        trend=ViolationTrend.NEW,
                    )
                )
        ordered = tuple(sorted(violations, key=lambda item: item.violation_id))
        if len(ordered) > limit:
            raise InvalidOperation("violation collection exceeds the requested bound")
        return ordered

    @_locked
    def compare_results(
        self,
        baseline_run_id: str,
        candidate_run_id: str,
        *,
        limit: int,
    ) -> ResultComparison:
        self._require_limit(limit)
        baseline = self._result_map(baseline_run_id)
        candidate = self._result_map(candidate_run_id)
        if baseline.keys() != candidate.keys():
            raise InvalidOperation("result snapshots do not contain the same metrics")
        voltage_deltas: list[MetricDelta] = []
        loading_deltas: list[MetricDelta] = []
        material: list[MetricDelta] = []
        for key in sorted(baseline):
            asset_id, metric = key
            asset = self._by_id[asset_id]
            before = baseline[key]
            after = candidate[key]
            delta_value = _decimal(after.value) - _decimal(before.value)
            delta = MetricDelta(asset, metric, before, after, _quantity(delta_value, before.unit))
            if metric == "voltage":
                voltage_deltas.append(delta)
                threshold = Decimal("0.005")
            else:
                loading_deltas.append(delta)
                threshold = Decimal("5")
            if abs(delta_value) >= threshold:
                material.append(delta)
        if len(voltage_deltas) + len(loading_deltas) > limit:
            raise InvalidOperation("result comparison exceeds the requested bound")
        baseline_ids = {
            item.violation_id
            for item in self.collect_violations(baseline_run_id, limit=self.max_page_size)
        }
        candidate_ids = {
            item.violation_id
            for item in self.collect_violations(candidate_run_id, limit=self.max_page_size)
        }
        return ResultComparison(
            comparison_id=f"comparison:{baseline_run_id}:{candidate_run_id}",
            baseline_run_id=baseline_run_id,
            candidate_run_id=candidate_run_id,
            convergence_change=ConvergenceChange.UNCHANGED_CONVERGED,
            voltage_deltas=tuple(voltage_deltas),
            loading_deltas=tuple(loading_deltas),
            added_violation_ids=tuple(sorted(candidate_ids - baseline_ids)),
            removed_violation_ids=tuple(sorted(baseline_ids - candidate_ids)),
            unchanged_violation_ids=tuple(sorted(baseline_ids & candidate_ids)),
            material_changes=tuple(material),
            materiality_policy=VersionedName("fake-materiality", "1.0.0"),
        )

    @_locked
    def plan_rollback(
        self,
        applied_change_id: str,
        *,
        expected_workflow_version: WorkflowVersion,
    ) -> RollbackPlan:
        applied = self._applied.get(applied_change_id)
        if applied is None:
            raise ObjectNotFound("applied change was not found")
        current_workflow = self._workflow_counters.get(expected_workflow_version.scope_id)
        if current_workflow is None or expected_workflow_version.counter != current_workflow:
            raise StaleContext("rollback workflow version is stale")
        restorations: list[RestorationValue] = []
        conflicts: list[RollbackConflict] = []
        for change in applied.affected_assets:
            attribute = change.confirmed[0].attribute
            expected = change.confirmed[0].value
            observed = self._values[change.asset.product_identity.value][attribute]
            restorations.append(RestorationValue(change.asset, attribute, observed, change.before[0].value))
            if observed != expected:
                conflicts.append(
                    RollbackConflict(change.asset, attribute, expected, observed, "live value changed after application")
                )
        fingerprint = self.live_state_fingerprint(
            self._configuration,
            tuple(item.asset for item in restorations),
            tuple(sorted({item.attribute for item in restorations})),
        )
        digest = ContentDigest(
            canonical_digest(
                (
                    applied_change_id,
                    tuple(
                        (
                            item.asset.product_identity.value,
                            item.attribute,
                            str(item.current.value),
                            str(item.restore_to.value),
                            item.current.unit,
                        )
                        for item in restorations
                    ),
                    fingerprint.value,
                    expected_workflow_version.wire,
                )
            )
        )
        return RollbackPlan(
            rollback_plan_id=f"rollback-plan:{applied_change_id}",
            workspace_id=_WORKSPACE_ID,
            workflow_id=expected_workflow_version.scope_id,
            applied_change_id=applied_change_id,
            conflicts=tuple(conflicts),
            workspace_disposition=WorkspaceDisposition.RESTORE,
            values_to_restore=tuple(restorations),
            validation_steps=("run deterministic load flow", "compare baseline"),
            configuration_key=self._configuration,
            live_state_fingerprint=fingerprint,
            workspace_revision=WorkspaceRevision(_WORKSPACE_ID, self._workspace_revision),
            expected_workflow_version=expected_workflow_version,
            expires_at=self._now() + timedelta(minutes=5),
            content_digest=digest,
        )

    @_locked
    def execute_rollback(
        self,
        plan: RollbackPlan,
        authorization: ExecutionAuthorization,
    ) -> RollbackResult:
        if authorization is None:
            raise AuthorizationRequired("a rollback authorization is required")
        self._require_registered_authorization(authorization)
        self._require_configuration(plan.configuration_key)
        if plan.expires_at <= self._now():
            raise StaleContext("rollback plan has expired")
        if plan.workspace_id != _WORKSPACE_ID:
            raise StaleContext("rollback plan is bound to another workspace")
        if plan.workspace_revision != WorkspaceRevision(_WORKSPACE_ID, self._workspace_revision):
            raise StaleContext("rollback workspace revision is stale")
        if self._workflow_counters.get(plan.workflow_id) != plan.expected_workflow_version.counter:
            raise StaleContext("rollback workflow version is stale")
        current = self.live_state_fingerprint(
            plan.configuration_key,
            tuple(item.asset for item in plan.values_to_restore),
            tuple(sorted({item.attribute for item in plan.values_to_restore})),
        )
        if current != plan.live_state_fingerprint:
            raise RollbackConflictError("rollback targets changed after the plan was created")
        if plan.conflicts:
            raise RollbackConflictError("rollback plan contains unresolved conflicts")
        for item in plan.values_to_restore:
            live = self._values[item.asset.product_identity.value][item.attribute]
            if live != item.current:
                raise RollbackConflictError("rollback precondition no longer matches live state")
        self._reserve_authorization(
            authorization,
            content_digest=plan.content_digest,
            configuration_key=plan.configuration_key,
            live_state_fingerprint=plan.live_state_fingerprint,
            operation_type=OperationType.ROLLBACK,
            expected_workflow_version=plan.expected_workflow_version,
        )
        restored: list[RestorationValue] = []
        for item in plan.values_to_restore:
            self._values[item.asset.product_identity.value][item.attribute] = item.restore_to
            restored.append(item)
        self._workspace_revision += 1
        self._workflow_counters[plan.workflow_id] += 1
        result = RollbackResult(
            rollback_result_id=f"rollback-result:{plan.applied_change_id}",
            workspace_id=_WORKSPACE_ID,
            rollback_plan_id=plan.rollback_plan_id,
            restored_values=tuple(restored),
            conflicts=(),
            verification_evidence=(
                VerificationEvidence("restored-values", True, "all fake values match their recorded baseline"),
            ),
            baseline_reproduced=None,
            completed_at=self._now(),
            verification_status=VerificationStatus.PARTIAL,
            workspace_revision=WorkspaceRevision(_WORKSPACE_ID, self._workspace_revision),
        )
        self._rollback_results[result.rollback_result_id] = result
        return result

    @_locked
    def verify_rollback_baseline(
        self,
        rollback_result_id: str,
        baseline_run_id: str,
        restored_run_id: str,
        *,
        limit: int,
    ) -> RollbackResult:
        self._require_limit(limit)
        result = self._rollback_results.get(rollback_result_id)
        if result is None:
            raise ObjectNotFound("rollback result was not found")
        comparison = self.compare_results(baseline_run_id, restored_run_id, limit=limit)
        reproduced = not comparison.material_changes and not comparison.added_violation_ids
        verified = replace(
            result,
            verification_evidence=result.verification_evidence
            + (
                VerificationEvidence(
                    "baseline-comparison",
                    reproduced,
                    "explicit post-rollback calculation comparison completed",
                ),
            ),
            baseline_reproduced=reproduced,
            verification_status=(VerificationStatus.VERIFIED if reproduced else VerificationStatus.FAILED),
        )
        self._rollback_results[rollback_result_id] = verified
        return verified

    @_locked
    def inject_partial_mutation(self, *, after_writes: int) -> None:
        """Interrupt the next mutation after N writes; intended only for tests."""
        if after_writes < 1:
            raise ValueError("after_writes must be positive")
        self._partial_mutation_after = after_writes

    @_locked
    def force_external_change(
        self,
        asset: AssetReference,
        attribute: str,
        value: Quantity,
    ) -> None:
        """Simulate an out-of-band writer; intended only for stale/conflict tests."""
        resolved = self._resolve(asset)
        current = self._values[resolved.product_identity.value].get(attribute)
        if current is None:
            raise InvalidOperation("cannot inject an unknown attribute")
        if current.unit != value.unit:
            raise InvalidOperation("external change must preserve the attribute unit")
        self._values[resolved.product_identity.value][attribute] = value

    @_locked
    def register_test_authorization(self, authorization: ExecutionAuthorization) -> None:
        """Register a pre-authorized value supplied by a test-only authority fixture."""
        if authorization.mutation_strategy is not MutationStrategy.DIRECT_LEDGER:
            raise AuthorizationInvalid("the fake supports direct-ledger test semantics only")
        if authorization.execution_id in self._registered_authorizations:
            raise AuthorizationInvalid("execution authorization is already registered")
        self._registered_authorizations[authorization.execution_id] = authorization
        self._execution_states[authorization.execution_id] = "registered"
        self._workflow_counters.setdefault(
            authorization.workflow_id,
            authorization.expected_workflow_version.counter,
        )

    def _record_applied(
        self,
        applied_change_id: str,
        authorization: ExecutionAuthorization,
        digest: ContentDigest,
        confirmed: Tuple[ConfirmedAssetChange, ...],
        status: VerificationStatus,
    ) -> AppliedChange:
        self._workspace_revision += 1
        self._workflow_counters[authorization.workflow_id] += 1
        applied = AppliedChange(
            applied_change_id=applied_change_id,
            workspace_id=_WORKSPACE_ID,
            execution_id=authorization.execution_id,
            proposal_digest=digest,
            mutation_strategy=authorization.mutation_strategy,
            affected_assets=confirmed,
            started_at=self._now(),
            completed_at=self._now(),
            verification_status=status,
            workspace_revision=WorkspaceRevision(_WORKSPACE_ID, self._workspace_revision),
        )
        self._applied[applied_change_id] = applied
        return applied

    def _reserve_authorization(
        self,
        authorization: ExecutionAuthorization,
        *,
        content_digest: ContentDigest,
        configuration_key: ConfigurationKey,
        live_state_fingerprint: LiveStateFingerprint,
        operation_type: OperationType,
        expected_workflow_version: WorkflowVersion,
    ) -> None:
        self._require_registered_authorization(authorization)
        if authorization.expires_at <= self._now():
            raise AuthorizationInvalid("execution authorization has expired")
        bindings = (
            authorization.proposal_digest == content_digest,
            authorization.configuration_key == configuration_key,
            authorization.live_state_fingerprint == live_state_fingerprint,
            authorization.operation_type == operation_type,
            authorization.mutation_strategy is MutationStrategy.DIRECT_LEDGER,
            authorization.expected_workflow_version == expected_workflow_version,
        )
        if not all(bindings):
            raise AuthorizationInvalid("execution authorization does not match the requested operation")
        self._execution_states[authorization.execution_id] = "consumed"

    def _require_registered_authorization(self, authorization: ExecutionAuthorization) -> None:
        if authorization.mutation_strategy is not MutationStrategy.DIRECT_LEDGER:
            raise AuthorizationInvalid("the fake supports direct-ledger test semantics only")
        registered = self._registered_authorizations.get(authorization.execution_id)
        if registered is None or registered != authorization:
            raise AuthorizationInvalid("execution authorization was not registered by the test authority")
        if self._execution_states.get(authorization.execution_id) != "registered":
            raise AuthorizationInvalid("execution authorization has already been reserved or consumed")

    def _result_map(self, run_id: str) -> dict[tuple[str, str], Quantity]:
        return {
            (asset.product_identity.value, metric.attribute): metric.value
            for asset, metrics in self.result_snapshot(run_id, limit=self.max_page_size)
            for metric in metrics
        }

    def _normalize_command_settings(
        self,
        command_settings: Tuple[CommandSetting, ...],
    ) -> Tuple[CommandSetting, ...]:
        if not isinstance(command_settings, tuple):
            raise InvalidOperation("command settings must be an immutable tuple")
        supplied: dict[str, CommandSetting] = {}
        for setting in command_settings:
            if setting.name in supplied:
                raise InvalidOperation("duplicate load-flow command setting")
            supplied[setting.name] = setting
        allowed = {setting.name: setting for setting in _DEFAULT_COMMAND_SETTINGS}
        unknown = set(supplied) - set(allowed)
        if unknown:
            raise InvalidOperation("unsupported load-flow command setting")
        normalized = {**allowed, **supplied}
        representation = normalized["network_representation"].value
        if representation != "balanced":
            raise InvalidOperation("the fake admits only balanced network representation")
        voltage_drops = normalized["calculate_voltage_drops"].value
        if not isinstance(voltage_drops, bool):
            raise InvalidOperation("calculate_voltage_drops must be boolean")
        tolerance = normalized["solver_tolerance"].value
        if not isinstance(tolerance, Quantity) or tolerance.unit != "p.u.":
            raise InvalidOperation("solver_tolerance must be a p.u. quantity")
        if not Decimal("0.000001") <= _decimal(tolerance.value) <= Decimal("0.01"):
            raise InvalidOperation("solver_tolerance is outside the fake's admitted range")
        return tuple(normalized[name] for name in sorted(normalized))

    def _calculation_input_payload(
        self,
        configuration_key: ConfigurationKey,
        settings: Tuple[CommandSetting, ...],
    ) -> dict[str, object]:
        loads = (self.load_1, self.load_2)
        attributes = ("active_power", "reactive_power")
        reads = self.read_attributes(
            configuration_key,
            loads,
            attributes,
            limit=_MAX_READ_CELLS,
        )
        fingerprint = self.live_state_fingerprint(configuration_key, loads, attributes)

        def setting_value(setting: CommandSetting) -> object:
            if isinstance(setting.value, Quantity):
                return {"value": str(setting.value.value), "unit": setting.value.unit}
            return setting.value

        return {
            "configuration_key": configuration_key.value,
            "workspace_revision": WorkspaceRevision(_WORKSPACE_ID, self._workspace_revision).wire,
            "dependency": {
                "completeness": CompletenessState.COMPLETE.value,
                "fingerprint": fingerprint.value,
                "live_values": tuple(
                    (
                        asset.product_identity.value,
                        tuple(
                            (item.attribute, str(item.value.value), item.value.unit)
                            for item in values
                        ),
                    )
                    for asset, values in reads
                ),
            },
            "locator_versions": tuple(
                (asset.product_identity.value, asset.locator.locator_version_id)
                for asset in (self.load_1, self.load_2, self.bus_1, self.bus_2, self.line)
            ),
            "command_settings": tuple((setting.name, setting_value(setting)) for setting in settings),
            "engineering_policy": (_POLICY.name, _POLICY.version),
            "result_schema": _RESULT_SCHEMA,
            "gateway_version": _FAKE_GATEWAY_VERSION,
        }

    def _encode_cursor(self, payload: dict[str, object]) -> PageCursor:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        payload_part = base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")
        signature = hmac.new(self._cursor_secret, payload_part.encode("ascii"), hashlib.sha256).digest()
        signature_part = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        return PageCursor(f"{payload_part}.{signature_part}")

    def _decode_cursor(self, cursor: PageCursor) -> dict[str, object]:
        try:
            payload_part, signature_part = cursor.token.split(".")
            supplied_signature = self._decode_base64url(signature_part)
            expected_signature = hmac.new(
                self._cursor_secret,
                payload_part.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise CursorInvalid("inventory cursor authentication failed")
            payload_bytes = self._decode_base64url(payload_part)
            payload = json.loads(payload_bytes.decode("utf-8"))
        except CursorInvalid:
            raise
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CursorInvalid("inventory cursor is malformed") from exc
        required = {
            "schema",
            "query_digest",
            "configuration_key",
            "extraction_revision",
            "sort",
            "last_product_uuid",
            "offset",
            "requested_page_size",
            "effective_page_size",
            "issued_at",
            "expires_at",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise CursorInvalid("inventory cursor payload has an invalid schema")
        for name in required - {"offset", "requested_page_size", "effective_page_size", "issued_at", "expires_at"}:
            if payload[name] is not None and not isinstance(payload[name], str):
                raise CursorInvalid("inventory cursor payload contains an invalid value")
        for name in ("offset", "requested_page_size", "effective_page_size", "issued_at", "expires_at"):
            if isinstance(payload[name], bool) or not isinstance(payload[name], int):
                raise CursorInvalid("inventory cursor payload contains an invalid integer")
        if payload["issued_at"] > payload["expires_at"]:
            raise CursorInvalid("inventory cursor lifetime is invalid")
        if int(self._now().timestamp()) >= payload["expires_at"]:
            raise CursorStale("inventory cursor has expired")
        return payload

    @staticmethod
    def _decode_base64url(value: str) -> bytes:
        if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
            raise CursorInvalid("inventory cursor encoding is invalid")
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
            raise CursorInvalid("inventory cursor encoding is not canonical")
        return decoded

    def _resolve(self, reference: AssetReference) -> AssetReference:
        resolved = self._by_id.get(reference.product_identity.value)
        if resolved is None:
            raise ObjectNotFound("asset product identity is unknown")
        if resolved != reference:
            raise StaleContext("asset reference does not match current locator evidence")
        return resolved

    def _require_configuration(self, configuration_key: ConfigurationKey) -> None:
        if configuration_key != self._configuration:
            raise ConfigurationMismatch("requested configuration is not active")

    def _require_limit(self, limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > self.max_page_size:
            raise InvalidOperation(f"limit must be between 1 and {self.max_page_size}")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fake gateway clock must return a timezone-aware datetime")
        return value


DeterministicFakeGateway = DeterministicHeadlineHarness


__all__ = ["DeterministicFakeGateway", "DeterministicHeadlineHarness"]
