"""Production composition for the read-only PowerFactory engineering MCP tools."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from collections.abc import Callable
import json
import os
import platform
import sys
import threading
import time
from uuid import uuid4

from powerfactory_agent.domain import (
    AssetKind,
    AssetReference,
    AttributeKind,
    AttributeSelector,
    CommandKind,
    CommandSelector,
    CompletenessState,
    ComponentsQuery,
    ContentDigest,
    ContextActivationObservation,
    ContextActivationRequest,
    ContextObservation,
    CleanupObservation,
    DependencyObservation,
    DependencyFingerprint,
    DependencyReadRequest,
    DependencySetIdentity,
    ExtractionProvenance,
    ExtractionRevision,
    FreshnessEvidence,
    FreshnessLevel,
    GraphAsset,
    GraphAttribute,
    GraphDataOrigin,
    GraphQuery,
    GraphQueryKind,
    GraphRelationship,
    GraphRelationshipKind,
    GraphSnapshot,
    IdentityLifecycleState,
    ImpactQuery,
    LoadFlowRequest,
    LocatorEvidenceSchema,
    LocatorKind,
    LocatorTrust,
    LiveStateFingerprint,
    MetricDefinition,
    MetricKind,
    ModelContext,
    NeighborhoodQuery,
    ObjectClassKind,
    ObjectClassSelector,
    ObjectObservation,
    ObjectQueryBatch,
    ObjectQueryRequest,
    ObjectQueryScope,
    OutOfServicePolicy,
    PowerFactoryLocator,
    PrimitiveObjectSelector,
    ProductIdentity,
    ProjectProvenance,
    Quantity,
    RelationshipKind,
    RelationshipSelector,
    ResultVariableKind,
    ResultVariableSelector,
    SessionObservation,
    SessionStartRequest,
    VersionedName,
)
from powerfactory_agent.gateway import (
    OperationResultUnavailableError,
    PowerFactoryGateway2026,
    SerializedPowerFactoryOwner,
)
from powerfactory_agent.gateway.native_powerfactory2026 import (
    NativePowerFactory2026Config,
    NativePowerFactory2026Vendor,
)
from powerfactory_agent.operations import LoadFlowService, PersistentModelGraph
from powerfactory_agent.persistence import (
    CalculationStore,
    GraphSnapshotNotFoundError,
    IdentityNotFoundError,
    IdentityStore,
    ModelGraphStore,
    OperationStore,
    SQLiteDatabase,
)
from powerfactory_agent.serialization import canonical_digest, to_primitive

from .configuration import McpInstallation, load_probe_config
from .component_catalog import SUPPORTED_CLASS_ASSET_KINDS, SUPPORTED_OBJECT_CLASSES
from .engineering import validate_component_list_request


_CONTRACT = VersionedName("powerfactory-2026-candidate-mapping", "0.1.0-unvalidated")
_DEPENDENCIES = DependencySetIdentity("powerfactory-model-inventory", "1")
_SUPPORTED_CLASSES = SUPPORTED_OBJECT_CLASSES
_KIND_MAP = dict(SUPPORTED_CLASS_ASSET_KINDS)
_ATTRIBUTES = (
    AttributeSelector(AttributeKind.DISPLAY_NAME, _CONTRACT),
    AttributeSelector(AttributeKind.NOMINAL_VOLTAGE, _CONTRACT),
    AttributeSelector(AttributeKind.ACTIVE_POWER, _CONTRACT),
    AttributeSelector(AttributeKind.REACTIVE_POWER, _CONTRACT),
)
_ATTRIBUTES_BY_CLASS = {
    ObjectClassKind.GRID: (_ATTRIBUTES[0],),
    ObjectClassKind.TERMINAL: (_ATTRIBUTES[0], _ATTRIBUTES[1]),
    ObjectClassKind.LINE: (_ATTRIBUTES[0],),
    ObjectClassKind.LOAD: (_ATTRIBUTES[0], _ATTRIBUTES[2], _ATTRIBUTES[3]),
    ObjectClassKind.TRANSFORMER: (_ATTRIBUTES[0],),
}
_CONNECTED_TERMINAL = RelationshipSelector(RelationshipKind.CONNECTED_TERMINAL, _CONTRACT)
_UNSUPPORTED_TOPOLOGY = ("ElmCoup switches", "ElmTr3 three-winding transformers")


class RuntimeOperationFailure(RuntimeError):
    """Sanitized failure with a durable evidence reference for an unavailable operation."""

    def __init__(self, diagnostic: dict[str, object]) -> None:
        super().__init__("PowerFactory operation requires investigation; see diagnostic evidence")
        self.diagnostic = diagnostic


class PowerFactoryEngineeringRuntime:
    """One lazily-created real session shared by all engineering MCP tools."""

    def __init__(self, installation: McpInstallation) -> None:
        self._installation = installation
        probe = load_probe_config(installation)
        self._probe = probe
        self._state_dir = installation.log_file.parent
        self._installation_id = f"powerfactory-2026:{canonical_digest(probe.pyd_path)[:24]}"
        self._profile_id = probe.user_profile_env_var or "default-profile"
        self._accepted_mappings = frozenset(
            {
                "context.active",
                "context.activate",
                "relationship.connected_terminal",
                "command.load_flow",
                *(f"class.{kind.value}" for kind in _SUPPORTED_CLASSES),
                *(f"attribute.{kind.value}" for kind in AttributeKind),
                "result.bus_voltage",
                "result.equipment_loading",
            }
        )
        self._context: ContextObservation | None = None
        self._started = False
        self._startup_lock = threading.Lock()
        # Startup-acquired collaborators remain None until successful explicit
        # startup so construction creates no database, thread, or native session.
        self._database: SQLiteDatabase | None = None
        self._owner: SerializedPowerFactoryOwner | None = None
        self._identity_store: IdentityStore | None = None
        self._graph_store: ModelGraphStore | None = None
        self._graph: PersistentModelGraph | None = None
        self._calculation_store: CalculationStore | None = None
        self._calculations: LoadFlowService | None = None
        self._session: SessionObservation | None = None
        self._extractor: _SupportedClassSnapshotExtractor | None = None

    def _start(self) -> None:
        """Explicit, idempotent runtime startup with side effects.

        Construction only captures validated configuration; database
        initialization, native gateway composition, serialized owner/worker
        creation, and session-start submission happen here, only after a
        confirmed context admission requests the first runtime operation. A
        partial failure cleans up resources actually acquired and retains
        sanitized failure evidence on the raised ``RuntimeOperationFailure``.
        ``powerfactory.pyd`` is only loaded through the serialized owner call
        path inside ``submit_start``.
        """

        with self._startup_lock:
            if self._started:
                return
            database = SQLiteDatabase(self._state_dir / "powerfactory-agent.sqlite3")
            vendor = NativePowerFactory2026Vendor(
                NativePowerFactory2026Config(
                    pyd_path=self._probe.pyd_path,
                    installation_id=self._installation_id,
                    profile_id=self._profile_id,
                    expected_python_abi=sys.implementation.cache_tag or "unknown",
                    expected_architecture=platform.machine(),
                    accepted_mappings=self._accepted_mappings,
                    cardinality_ceiling=self._probe.cardinality_ceiling,
                    user_profile_env_var=self._probe.user_profile_env_var,
                    password_env_var=self._probe.password_env_var,
                )
            )
            gateway = PowerFactoryGateway2026(vendor)
            owner = SerializedPowerFactoryOwner(
                gateway,
                OperationStore(database),
                max_queue_size=64,
                queue_deadline_ms=30_000,
                client_response_deadline_ms=60_000,
                engine_health_threshold_ms=120_000,
            )
            self._database = database
            self._owner = owner
            self._identity_store = IdentityStore(database)
            self._graph_store = ModelGraphStore(database)
            self._graph = PersistentModelGraph(self._graph_store)
            self._calculation_store = CalculationStore(database)
            self._calculations = LoadFlowService(
                owner,
                self._calculation_store,
                owner_wait_timeout_seconds=120.0,
            )
            self._extractor = _SupportedClassSnapshotExtractor(
                owner,
                self._identity_store,
                self._graph_store,
                self._installation_id,
                self._profile_id,
                self._await,
            )
            try:
                self._session = self._await(
                    owner.submit_start(
                        SessionStartRequest(
                            self._installation_id,
                            self._profile_id,
                            "2026",
                            "configured",
                            True,
                        ),
                        idempotency_key=f"session-start:{uuid4()}",
                    ),
                    SessionObservation,
                )
            except Exception:
                self._session = None
                owner.shutdown_serialization(timeout_ms=5_000)
                self._owner = None
                self._database = None
                self._identity_store = None
                self._graph_store = None
                self._graph = None
                self._calculation_store = None
                self._calculations = None
                self._extractor = None
                raise
            self._started = True

    def activate_context(self, *, project_selector: str, study_case: str) -> dict[str, object]:
        """Activate and verify the explicitly admitted project context for this process only."""

        if not project_selector.strip() or not study_case.strip():
            raise ValueError("project_selector and study_case must be non-empty")
        if self._context is not None:
            raise ValueError("an active PowerFactory context already exists for this MCP session")
        self._start()
        activated = self._await(
            self._owner.submit_activate_context(
                ContextActivationRequest(project_selector, study_case, None),
                idempotency_key=f"context-activate:{uuid4()}",
            ),
            ContextActivationObservation,
        )
        context = activated.context
        if not context.verified or context.configuration_key is None or context.project_key is None:
            raise RuntimeError("PowerFactory has no verified active project, study case, and grid context")
        self._context = context
        return self._response("verified-context-admission", context=to_primitive(context))

    def get_model_context(self) -> dict[str, object]:
        snapshot = self._ensure_snapshot()
        return self._response(
            "verified-live-read",
            context=to_primitive(snapshot.context),
            topology_complete=False,
            unsupported_topology=list(_UNSUPPORTED_TOPOLOGY),
        )

    def list_components(self, *, asset_kind: str, limit: int, cursor: str | None) -> dict[str, object]:
        validate_component_list_request(asset_kind=asset_kind, limit=limit)
        kind = AssetKind(asset_kind)
        offset = 0 if cursor is None else self._decode_cursor(cursor)
        assets = [item.asset for item in self._ensure_snapshot().assets if item.asset.asset_kind is kind]
        page = assets[offset:offset + limit]
        next_offset = offset + len(page)
        return self._response(
            "persisted-live-extraction",
            components=to_primitive(tuple(page)),
            next_cursor=None if next_offset >= len(assets) else f"offset:{next_offset}",
            total=len(assets),
        )

    def get_asset_context(self, *, product_identity: str) -> dict[str, object]:
        identity = ProductIdentity(product_identity)
        binding = self._identity_store.get(identity)
        snapshot = self._ensure_snapshot()
        graph_asset = next(
            (item for item in snapshot.assets if item.asset.product_identity == identity),
            None,
        )
        if graph_asset is None:
            raise IdentityNotFoundError("identity is not present in the active model extraction")
        attributes = tuple(item for item in snapshot.attributes if item.asset_identity == identity)
        relationships = tuple(
            item for item in snapshot.relationships
            if identity in {item.source_identity, item.target_identity}
        )
        return self._response(
            "persisted-live-extraction",
            binding=to_primitive(binding.current_locator),
            lifecycle_state=binding.lifecycle_state.value,
            asset=to_primitive(graph_asset),
            attributes=to_primitive(attributes),
            relationships=to_primitive(relationships),
        )

    def run_validated_load_flow(self, *, idempotency_key: str) -> dict[str, object]:
        if not idempotency_key or len(idempotency_key) > 128:
            raise ValueError("idempotency_key must be 1-128 characters")
        snapshot = self._ensure_snapshot()
        metrics: list[MetricDefinition] = []
        for graph_asset in snapshot.assets:
            asset = graph_asset.asset
            if asset.asset_kind is AssetKind.TERMINAL:
                metrics.append(self._metric(asset, MetricKind.BUS_VOLTAGE, "p.u."))
            elif asset.asset_kind in {AssetKind.LINE, AssetKind.TRANSFORMER}:
                metrics.append(self._metric(asset, MetricKind.EQUIPMENT_LOADING, "%"))
        if not metrics:
            raise ValueError("active model has no supported load-flow result assets")
        metrics = sorted(metrics, key=lambda item: item.definition_id)
        metrics_truncated = len(metrics) > 100
        metrics = metrics[:100]
        request = LoadFlowRequest(
            snapshot.context.model_context_id,
            snapshot.context.configuration_key,
            snapshot.context.extraction_revision,
            CommandSelector(CommandKind.LOAD_FLOW, _CONTRACT),
            (),
            tuple(metrics),
            VersionedName("observation-only-no-engineering-limits", "1"),
            idempotency_key,
        )
        run = self._calculations.run_validated_load_flow(request)
        return self._response(
            "real-powerfactory-calculation",
            calculation_run=to_primitive(run),
            metrics_captured=len(metrics),
            metrics_truncated=metrics_truncated,
            policy_note="Results are captured; engineering limits are not evaluated in this release.",
        )

    def get_calculation_run(self, *, run_id: str) -> dict[str, object]:
        run = self._calculations.get_calculation_run(run_id)
        payload: dict[str, object] = {"calculation_run": to_primitive(run)}
        if run.result_snapshot_id is not None:
            payload["result_snapshot"] = to_primitive(self._calculation_store.snapshot(run.result_snapshot_id))
        return self._response("persisted-calculation-evidence", **payload)

    def compare_results(self, *, baseline_snapshot_id: str, candidate_snapshot_id: str) -> dict[str, object]:
        comparison = self._calculations.compare_snapshots(baseline_snapshot_id, candidate_snapshot_id)
        return self._response("persisted-calculation-comparison", comparison=to_primitive(comparison))

    def refresh_model_graph(self) -> dict[str, object]:
        snapshot = self._extractor.extract_snapshot(self._require_context(), self._session)
        self._graph.full_refresh(snapshot)
        return self._graph_summary(snapshot)

    def get_model_graph_summary(self) -> dict[str, object]:
        return self._graph_summary(self._ensure_snapshot())

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
    ) -> dict[str, object]:
        del source_identity, target_identity
        if query_kind not in {"components", "neighborhood", "impact"}:
            raise ValueError(
                "query_kind is unavailable for the incomplete topology catalog; use components, neighborhood, or impact"
            )
        kind = GraphQueryKind(query_kind)
        query = GraphQuery(kind, model_context_id, ExtractionRevision(model_context_id, extraction_revision), limit)
        if kind is GraphQueryKind.COMPONENTS:
            result = self._graph.connected_components(ComponentsQuery(query))
        else:
            if center_identity is None:
                raise ValueError("center_identity is required")
            identity = ProductIdentity(center_identity)
            result = (
                self._graph.neighborhood(NeighborhoodQuery(query, identity, hops))
                if kind is GraphQueryKind.NEIGHBORHOOD
                else self._graph.impact(ImpactQuery(query, identity, hops))
            )
        return self._response(
            "bounded-incomplete-topology",
            result=to_primitive(result),
            topology_complete=False,
            unsupported_topology=list(_UNSUPPORTED_TOPOLOGY),
        )

    def close(self) -> None:
        owner = self._owner
        if owner is None:
            # Construction is side-effect free and a failed startup already
            # released its owner; close before startup is a harmless no-op.
            return
        if self._started:
            try:
                record = owner.submit_close(idempotency_key=f"session-close:{uuid4()}")
                self._await(record, CleanupObservation)
            finally:
                owner.shutdown_serialization(timeout_ms=5_000)
        else:
            owner.shutdown_serialization(timeout_ms=5_000)

    def _require_context(self) -> ContextObservation:
        if self._context is None:
            raise ValueError("CONTEXT_REQUIRED: call open_project_context with an exact confirmed project and study case")
        return self._context

    def _ensure_snapshot(self) -> GraphSnapshot:
        context = self._require_context()
        try:
            return self._graph_store.latest(expected_configuration_key=context.configuration_key.value)
        except GraphSnapshotNotFoundError:
            snapshot = self._extractor.extract_snapshot(context, self._session)
            return self._graph.full_refresh(snapshot)

    @staticmethod
    def _metric(asset: AssetReference, metric_kind: MetricKind, unit: str) -> MetricDefinition:
        variable_kind = (
            ResultVariableKind.BUS_VOLTAGE
            if metric_kind is MetricKind.BUS_VOLTAGE
            else ResultVariableKind.EQUIPMENT_LOADING
        )
        zero = Quantity(Decimal("0"), unit)
        if metric_kind is MetricKind.BUS_VOLTAGE:
            object_class = ObjectClassKind.TERMINAL
        elif asset.asset_kind is AssetKind.LINE:
            object_class = ObjectClassKind.LINE
        else:
            object_class = ObjectClassKind.TRANSFORMER
        return MetricDefinition(
            f"{metric_kind.value}:{asset.product_identity.value}",
            asset.product_identity,
            metric_kind,
            PrimitiveObjectSelector(
                asset.project_key,
                ObjectClassSelector(object_class, _CONTRACT),
                asset.locator.native_field,
                asset.locator.native_value,
                asset.locator.canonical_path,
            ),
            ResultVariableSelector(variable_kind, _CONTRACT),
            unit,
            None,
            None,
            zero,
            Quantity(Decimal("0.0001"), unit),
            Quantity(Decimal("0.001"), unit),
            "not configured; observation only",
        )

    def _graph_summary(self, snapshot: GraphSnapshot) -> dict[str, object]:
        return self._response(
            "persisted-supported-class-topology",
            model_context_id=snapshot.context.model_context_id,
            extraction_revision=snapshot.context.extraction_revision.counter,
            assets=len(snapshot.assets),
            relationships=len(snapshot.relationships),
            topology_complete=False,
            unsupported_topology=list(_UNSUPPORTED_TOPOLOGY),
        )

    def _await(self, record: object, result_type: type[object]) -> object:
        operation_id = getattr(record, "operation_id")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = self._owner.status(operation_id)
            if status.terminal:
                try:
                    return self._owner.completed_result(operation_id, result_type)
                except OperationResultUnavailableError as exc:
                    raise RuntimeOperationFailure(self._persist_operation_failure(exc)) from exc
            time.sleep(0.005)
        raise RuntimeOperationFailure(self._persist_timeout(operation_id))

    def _persist_operation_failure(
        self,
        exc: OperationResultUnavailableError,
    ) -> dict[str, object]:
        record = exc.record
        error = record.error if isinstance(record.error, dict) else {}
        return self._persist_runtime_diagnostic(
            {
                "operation_id": record.operation_id,
                "handler": record.handler_name,
                "state": record.state.value,
                "exception_category": self._safe_classification(error.get("category")),
                "exception_type": self._safe_classification(error.get("exception_type")),
            }
        )

    def _persist_timeout(self, operation_id: str) -> dict[str, object]:
        return self._persist_runtime_diagnostic(
            {
                "operation_id": operation_id,
                "handler": "unavailable",
                "state": "CLIENT_WAIT_TIMEOUT",
                "exception_category": "client_wait_timeout",
                "exception_type": "TimeoutError",
            }
        )

    def _persist_runtime_diagnostic(self, operation: dict[str, str]) -> dict[str, object]:
        return _write_runtime_diagnostic(self._installation, operation, self._owner.diagnostics())

    @staticmethod
    def _safe_classification(value: object) -> str:
        if not isinstance(value, str):
            return "unavailable"
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
        return value if 0 < len(value) <= 128 and set(value) <= allowed else "unavailable"

    @staticmethod
    def _decode_cursor(cursor: str) -> int:
        prefix, separator, value = cursor.partition(":")
        if prefix != "offset" or separator != ":" or not value.isdigit():
            raise ValueError("cursor is invalid")
        return int(value)

    @staticmethod
    def _response(evidence: str, **payload: object) -> dict[str, object]:
        return {
            "status": "OK",
            "source": "real-powerfactory",
            "evidence": evidence,
            **payload,
        }


__all__ = ["PowerFactoryEngineeringRuntime", "RuntimeOperationFailure"]


def _write_runtime_diagnostic(
    installation: McpInstallation,
    operation: dict[str, str],
    owner_diagnostics: dict[str, object],
) -> dict[str, object]:
    """Persist a sanitized runtime diagnostic evidence file and return it.

    Extracted from the runtime facade so diagnostic persistence is cohesive and
    testable without composing a full runtime. The structure, ordering,
    evidence_id format, and write semantics are preserved verbatim.
    """

    evidence_id = (
        f"runtime-failure-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
        f"{operation['operation_id']}.json"
    )
    diagnostic: dict[str, object] = {
        "schema_version": "powerfactory-runtime-diagnostic/v1",
        "evidence_id": evidence_id,
        "operation": operation,
        "owner": owner_diagnostics,
        "mcp_process": {"pid": os.getpid(), "alive": True},
    }
    directory = installation.log_file.parent / "evidence"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / evidence_id).write_text(
        json.dumps(diagnostic, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return diagnostic


class _SupportedClassSnapshotExtractor:
    """Supported-class subgraph extraction collaborator for the engineering runtime.

    Builds a bounded ``GraphSnapshot`` of admitted classes, attributes, and
    connected-terminal relationships through the serialized owner. The body is
    preserved verbatim from the prior facade methods; only the owner, stores,
    installation identity, session, and an await callable are injected so the
    facade stays a composition boundary and never exposes vendor handles.
    """

    def __init__(
        self,
        owner: SerializedPowerFactoryOwner,
        identity_store: IdentityStore,
        graph_store: ModelGraphStore,
        installation_id: str,
        profile_id: str,
        await_operation: Callable[[object, type[object]], object],
    ) -> None:
        self._owner = owner
        self._identity_store = identity_store
        self._graph_store = graph_store
        self._installation_id = installation_id
        self._profile_id = profile_id
        self._await_operation = await_operation

    def extract_snapshot(
        self,
        context: ContextObservation,
        session: SessionObservation,
    ) -> GraphSnapshot:
        active_context = context
        records = self._query_all_objects(context)
        try:
            prior = self._graph_store.latest(
                expected_configuration_key=active_context.configuration_key.value
            )
            context_id = prior.context.model_context_id
            counter = prior.context.extraction_revision.counter + 1
        except GraphSnapshotNotFoundError:
            context_id, counter = str(uuid4()), 1
        evidence_reference = f"gateway-session:{session.session_id}:extraction:{counter}"
        assets_by_selector: dict[PrimitiveObjectSelector, AssetReference] = {}
        attributes: list[GraphAttribute] = []
        for record in records:
            locator = self._locator(record.selector, record.selector.object_class.kind.value, session)
            try:
                binding = self._identity_store.resolve_exact(locator)
            except IdentityNotFoundError:
                binding = self._identity_store.create(locator, evidence_reference=evidence_reference)
            asset = AssetReference(
                binding.product_identity,
                binding.current_locator,
                record.display_name,
                _KIND_MAP[record.selector.object_class.kind],
                record.selector.project_key,
                IdentityLifecycleState.ACTIVE,
            )
            assets_by_selector[record.selector] = asset
            for field in record.fields:
                attributes.append(
                    GraphAttribute(
                        asset.product_identity,
                        field.selector.kind.value,
                        str(to_primitive(field.value)),
                        GraphDataOrigin.EXTRACTED,
                    )
                )
        relationships = self._extract_relationships(context, assets_by_selector)
        ordered_assets = tuple(
            sorted(assets_by_selector.values(), key=lambda item: item.product_identity.value)
        )
        fingerprint_value = canonical_digest(
            {"configuration_key": active_context.configuration_key, "assets": ordered_assets},
            kind="live-state-fingerprint",
        )
        fingerprint = LiveStateFingerprint(fingerprint_value)
        now = datetime.now(timezone.utc)
        dependency = DependencyFingerprint(
            _DEPENDENCIES,
            fingerprint,
            CompletenessState.CONSERVATIVE,
            now,
            session.session_id,
            evidence_reference,
            VersionedName("supported-class-inventory", "1"),
        )
        freshness = FreshnessEvidence(
            FreshnessLevel.VERIFIED,
            now,
            session.session_id,
            active_context.configuration_key,
            _DEPENDENCIES,
            evidence_reference,
            "supported-class-inventory",
            "1",
            False,
        )
        model_context = ModelContext(
            context_id,
            active_context.configuration_key,
            session.powerfactory_version,
            ordered_assets,
            ExtractionRevision(context_id, counter),
            now,
            freshness,
            (dependency,),
        )
        graph_assets = tuple(
            sorted(
                (
                    GraphAsset(
                        asset,
                        None,
                        None,
                        True,
                        False,
                        None,
                        2 if asset.asset_kind is AssetKind.TRANSFORMER else 0,
                    )
                    for asset in ordered_assets
                ),
                key=lambda item: item.asset.product_identity.value,
            )
        )
        provenance = (
            ExtractionProvenance(
                "PowerFactory 2026 native gateway",
                "Supported class subgraph only; switch, three-winding-transformer, and explicit out-of-service state mappings are not admitted.",
                GraphDataOrigin.EXTRACTED,
            ),
        )
        content = {
            "context": model_context,
            "assets": graph_assets,
            "attributes": tuple(attributes),
            "relationships": relationships,
        }
        return GraphSnapshot(
            str(uuid4()),
            model_context,
            ContentDigest(canonical_digest(content)),
            graph_assets,
            tuple(sorted(attributes, key=lambda item: (item.asset_identity.value, item.name))),
            relationships,
            provenance,
        )

    def _query_all_objects(self, context: ContextObservation) -> tuple[ObjectObservation, ...]:
        records: list[ObjectObservation] = []
        for object_class in _SUPPORTED_CLASSES:
            cursor = None
            while True:
                request = ObjectQueryRequest(
                    context.configuration_key,
                    ObjectQueryScope.ACTIVE_GRIDS,
                    OutOfServicePolicy.EXCLUDE,
                    (ObjectClassSelector(object_class, _CONTRACT),),
                    _ATTRIBUTES_BY_CLASS[object_class],
                    100,
                    cursor,
                )
                batch = self._await_operation(
                    self._owner.submit_query_objects(request, idempotency_key=f"inventory:{uuid4()}"),
                    ObjectQueryBatch,
                )
                records.extend(batch.records)
                if batch.complete:
                    break
                cursor = batch.next_cursor
        return tuple(records)

    def _extract_relationships(
        self,
        context: ContextObservation,
        assets: dict[PrimitiveObjectSelector, AssetReference],
    ) -> tuple[GraphRelationship, ...]:
        equipment = [
            selector
            for selector in assets
            if selector.object_class.kind
            in {ObjectClassKind.LINE, ObjectClassKind.LOAD, ObjectClassKind.TRANSFORMER}
        ]
        relationships: list[GraphRelationship] = []
        for offset in range(0, len(equipment), 100):
            selected = tuple(equipment[offset:offset + 100])
            observation = self._await_operation(
                self._owner.submit_observe_dependencies(
                    DependencyReadRequest(
                        context.configuration_key,
                        selected,
                        (),
                        (_CONNECTED_TERMINAL,),
                        100,
                    ),
                    idempotency_key=f"topology:{uuid4()}",
                ),
                DependencyObservation,
            )
            if not observation.complete:
                raise RuntimeError("PowerFactory topology dependency read was incomplete")
            for item in observation.objects:
                source = assets[item.selector]
                for edge in item.relationships:
                    target = assets.get(edge.target)
                    if target is None:
                        raise RuntimeError("topology references a terminal outside the admitted inventory")
                    edge_id = canonical_digest(
                        {"source": source.product_identity, "target": target.product_identity},
                        kind="relationship-id",
                    )
                    relationships.append(
                        GraphRelationship(
                            edge_id,
                            source.product_identity,
                            target.product_identity,
                            GraphRelationshipKind.CONNECTS,
                            GraphDataOrigin.EXTRACTED,
                            True,
                        )
                    )
        return tuple(sorted(relationships, key=lambda item: item.relationship_id))

    def _locator(
        self,
        selector: PrimitiveObjectSelector,
        object_class: str,
        session: SessionObservation,
    ) -> PowerFactoryLocator:
        return PowerFactoryLocator(
            str(uuid4()),
            LocatorKind.NATIVE_CANDIDATE if selector.native_value is not None else LocatorKind.CANONICAL_PATH_FALLBACK,
            ProjectProvenance(
                self._installation_id,
                self._profile_id,
                selector.project_key,
                f"gateway-session:{session.session_id}",
            ),
            object_class,
            selector.native_field,
            selector.native_value,
            selector.canonical_path,
            LocatorEvidenceSchema("powerfactory-object-selector", "1", session.adapter_version),
            datetime.now(timezone.utc),
            session.session_id,
            LocatorTrust.CANDIDATE if selector.native_value is not None else LocatorTrust.FALLBACK,
            False,
        )
