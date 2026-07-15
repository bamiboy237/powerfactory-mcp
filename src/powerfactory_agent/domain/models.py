"""Immutable contracts for the PowerFactory-independent product core."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from .values import (
    AssetKind,
    CalculationInputDigest,
    CompletenessState,
    ConfigurationKey,
    ContentDigest,
    ConvergenceChange,
    ConvergenceState,
    DependencySetIdentity,
    ExtractionRevision,
    FreshnessEvidence,
    FreshnessLevel,
    IdentityLifecycleState,
    LiveStateFingerprint,
    MutationStrategy,
    OperationType,
    PowerFactoryLocator,
    ProductIdentity,
    Quantity,
    VerificationStatus,
    ViolationSeverity,
    ViolationTrend,
    WorkflowVersion,
    WorkspaceDisposition,
    WorkspaceRevision,
    require_aware,
    require_collection,
    require_text,
)


@dataclass(frozen=True, slots=True)
class VersionedName:
    name: str
    version: str

    def __post_init__(self) -> None:
        require_text(self.name, "VersionedName.name", maximum=128)
        require_text(self.version, "VersionedName.version", maximum=128)


@dataclass(frozen=True, slots=True)
class NamedValue:
    name: str
    value: str

    def __post_init__(self) -> None:
        require_text(self.name, "NamedValue.name", maximum=128)
        require_text(self.value, "NamedValue.value")


@dataclass(frozen=True, slots=True)
class DependencyFingerprint:
    dependency_set: DependencySetIdentity
    fingerprint: LiveStateFingerprint
    completeness: CompletenessState
    observed_at: datetime
    session_id: str
    evidence_reference: str
    policy: VersionedName

    def __post_init__(self) -> None:
        require_aware(self.observed_at, "DependencyFingerprint.observed_at")
        require_text(self.session_id, "DependencyFingerprint.session_id", maximum=256)
        require_text(self.evidence_reference, "DependencyFingerprint.evidence_reference")


@dataclass(frozen=True, slots=True)
class AssetReference:
    product_identity: ProductIdentity
    locator: PowerFactoryLocator
    display_name: str
    asset_kind: AssetKind
    project_key: str
    lifecycle_state: IdentityLifecycleState

    def __post_init__(self) -> None:
        require_text(self.display_name, "AssetReference.display_name")
        require_text(self.project_key, "AssetReference.project_key")
        if self.project_key != self.locator.project_provenance.project_key:
            raise ValueError("AssetReference project_key must match its locator")


@dataclass(frozen=True, slots=True)
class ModelContext:
    model_context_id: str
    configuration_key: ConfigurationKey
    powerfactory_version: str
    assets: Tuple[AssetReference, ...]
    extraction_revision: ExtractionRevision
    extracted_at: datetime
    freshness: FreshnessEvidence
    dependency_fingerprints: Tuple[DependencyFingerprint, ...]

    def __post_init__(self) -> None:
        from .values import require_uuid4

        require_uuid4(self.model_context_id, "ModelContext.model_context_id")
        if self.extraction_revision.scope_id != self.model_context_id:
            raise ValueError("extraction_revision scope must equal model_context_id")
        if self.freshness.configuration_key != self.configuration_key:
            raise ValueError("freshness evidence must bind the context configuration_key")
        if self.freshness.level in (FreshnessLevel.VERIFIED, FreshnessLevel.LIVE):
            if not self.dependency_fingerprints:
                raise ValueError("VERIFIED/LIVE context requires dependency fingerprint evidence")
            for dependency in self.dependency_fingerprints:
                if dependency.completeness is CompletenessState.UNSUPPORTED:
                    raise ValueError("unsupported dependency evidence cannot claim VERIFIED/LIVE freshness")
                if dependency.session_id != self.freshness.session_id:
                    raise ValueError("freshness and dependency evidence must use the same session")
                if dependency.dependency_set != self.freshness.dependency_set:
                    raise ValueError("freshness and dependency evidence must bind the same dependency set")
        require_text(self.powerfactory_version, "ModelContext.powerfactory_version", maximum=128)
        require_collection(self.assets, "ModelContext.assets")
        require_collection(self.dependency_fingerprints, "ModelContext.dependency_fingerprints")
        require_aware(self.extracted_at, "ModelContext.extracted_at")


@dataclass(frozen=True, slots=True)
class AttributeQuantity:
    attribute: str
    value: Quantity

    def __post_init__(self) -> None:
        require_text(self.attribute, "AttributeQuantity.attribute", maximum=256)


@dataclass(frozen=True, slots=True)
class ProposedAssetChange:
    asset: AssetReference
    before: Tuple[AttributeQuantity, ...]
    proposed: Tuple[AttributeQuantity, ...]

    def __post_init__(self) -> None:
        require_collection(self.before, "ProposedAssetChange.before", allow_empty=False)
        require_collection(self.proposed, "ProposedAssetChange.proposed", allow_empty=False)
        before_units = {item.attribute: item.value.unit for item in self.before}
        if len(before_units) != len(self.before):
            raise ValueError("ProposedAssetChange.before contains duplicate attributes")
        proposed_units = {item.attribute: item.value.unit for item in self.proposed}
        if len(proposed_units) != len(self.proposed):
            raise ValueError("ProposedAssetChange.proposed contains duplicate attributes")
        if before_units != proposed_units:
            raise ValueError("before and proposed attributes and units must match")


@dataclass(frozen=True, slots=True)
class EngineeringOperation:
    operation_type: OperationType
    operation_specification: VersionedName
    parameters: Tuple[NamedValue | AttributeQuantity, ...]

    def __post_init__(self) -> None:
        require_collection(self.parameters, "EngineeringOperation.parameters", allow_empty=False)


@dataclass(frozen=True, slots=True)
class ChangePreview:
    preview_id: str
    model_context_id: str
    workspace_id: str
    workflow_id: str
    operation: EngineeringOperation
    resolved_changes: Tuple[ProposedAssetChange, ...]
    selection_criteria: Tuple[NamedValue, ...]
    warnings: Tuple[str, ...]
    exclusions: Tuple[str, ...]
    configuration_key: ConfigurationKey
    live_state_fingerprint: LiveStateFingerprint
    extraction_revision: ExtractionRevision
    workspace_revision: WorkspaceRevision
    expected_workflow_version: WorkflowVersion
    engineering_policy: VersionedName
    expires_at: datetime
    content_digest: ContentDigest
    required_validation_steps: Tuple[str, ...]

    def __post_init__(self) -> None:
        from .values import require_uuid4

        require_text(self.preview_id, "ChangePreview.preview_id", maximum=128)
        require_uuid4(self.model_context_id, "ChangePreview.model_context_id")
        require_uuid4(self.workspace_id, "ChangePreview.workspace_id")
        require_uuid4(self.workflow_id, "ChangePreview.workflow_id")
        if self.extraction_revision.scope_id != self.model_context_id:
            raise ValueError("extraction_revision scope must equal model_context_id")
        if self.workspace_revision.scope_id != self.workspace_id:
            raise ValueError("workspace_revision scope must equal workspace_id")
        if self.expected_workflow_version.scope_id != self.workflow_id:
            raise ValueError("workflow version scope must equal workflow_id")
        require_collection(self.resolved_changes, "ChangePreview.resolved_changes", allow_empty=False)
        require_collection(self.selection_criteria, "ChangePreview.selection_criteria")
        require_collection(self.warnings, "ChangePreview.warnings")
        require_collection(self.exclusions, "ChangePreview.exclusions")
        require_collection(
            self.required_validation_steps,
            "ChangePreview.required_validation_steps",
            allow_empty=False,
        )
        for name, values in (
            ("warnings", self.warnings),
            ("exclusions", self.exclusions),
            ("required_validation_steps", self.required_validation_steps),
        ):
            for value in values:
                require_text(value, f"ChangePreview.{name} entry")
        require_aware(self.expires_at, "ChangePreview.expires_at")


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    approval_request_id: str
    preview_id: str
    proposal_digest: ContentDigest
    requested_at: datetime
    expires_at: datetime
    agent_identity: str
    client_identity: str

    def __post_init__(self) -> None:
        for field_name in ("approval_request_id", "preview_id", "agent_identity", "client_identity"):
            require_text(getattr(self, field_name), f"ApprovalRequest.{field_name}", maximum=256)
        require_aware(self.requested_at, "ApprovalRequest.requested_at")
        require_aware(self.expires_at, "ApprovalRequest.expires_at")
        if self.expires_at <= self.requested_at:
            raise ValueError("ApprovalRequest expires_at must follow requested_at")


@dataclass(frozen=True, slots=True)
class ExecutionAuthorization:
    execution_id: str
    workflow_id: str
    approval_request_id: str
    authenticated_principal: str
    proposal_digest: ContentDigest
    configuration_key: ConfigurationKey
    live_state_fingerprint: LiveStateFingerprint
    operation_type: OperationType
    mutation_strategy: MutationStrategy
    expected_workflow_version: WorkflowVersion
    issued_at: datetime
    expires_at: datetime
    agent_identity: str
    client_identity: str

    def __post_init__(self) -> None:
        from .values import require_uuid4

        for field_name in (
            "execution_id",
            "approval_request_id",
            "authenticated_principal",
            "agent_identity",
            "client_identity",
        ):
            require_text(getattr(self, field_name), f"ExecutionAuthorization.{field_name}", maximum=256)
        require_aware(self.issued_at, "ExecutionAuthorization.issued_at")
        require_aware(self.expires_at, "ExecutionAuthorization.expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("ExecutionAuthorization expires_at must follow issued_at")
        require_uuid4(self.workflow_id, "ExecutionAuthorization.workflow_id")
        if self.expected_workflow_version.scope_id != self.workflow_id:
            raise ValueError("workflow version scope must equal workflow_id")


@dataclass(frozen=True, slots=True)
class ConfirmedAssetChange:
    asset: AssetReference
    before: Tuple[AttributeQuantity, ...]
    proposed: Tuple[AttributeQuantity, ...]
    confirmed: Tuple[AttributeQuantity, ...]

    def __post_init__(self) -> None:
        require_collection(self.before, "ConfirmedAssetChange.before", allow_empty=False)
        require_collection(self.proposed, "ConfirmedAssetChange.proposed", allow_empty=False)
        require_collection(self.confirmed, "ConfirmedAssetChange.confirmed", allow_empty=False)
        signatures = []
        for group in (self.before, self.proposed, self.confirmed):
            signature = tuple((item.attribute, item.value.unit) for item in group)
            if len(set(signature)) != len(signature):
                raise ValueError("ConfirmedAssetChange contains duplicate attribute/unit pairs")
            signatures.append(signature)
        if signatures[0] != signatures[1] or signatures[1] != signatures[2]:
            raise ValueError("before, proposed, and confirmed values must have identical attributes and units")


@dataclass(frozen=True, slots=True)
class AppliedChange:
    applied_change_id: str
    workspace_id: str
    execution_id: str
    proposal_digest: ContentDigest
    mutation_strategy: MutationStrategy
    affected_assets: Tuple[ConfirmedAssetChange, ...]
    started_at: datetime
    completed_at: datetime
    verification_status: VerificationStatus
    workspace_revision: WorkspaceRevision

    def __post_init__(self) -> None:
        from .values import require_uuid4

        require_text(self.applied_change_id, "AppliedChange.applied_change_id", maximum=128)
        require_text(self.execution_id, "AppliedChange.execution_id", maximum=128)
        require_collection(self.affected_assets, "AppliedChange.affected_assets", allow_empty=False)
        require_aware(self.started_at, "AppliedChange.started_at")
        require_aware(self.completed_at, "AppliedChange.completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("AppliedChange completed_at cannot precede started_at")
        require_uuid4(self.workspace_id, "AppliedChange.workspace_id")
        if self.workspace_revision.scope_id != self.workspace_id:
            raise ValueError("workspace_revision scope must equal workspace_id")


@dataclass(frozen=True, slots=True)
class CommandSetting:
    name: str
    value: str | bool | Quantity

    def __post_init__(self) -> None:
        require_text(self.name, "CommandSetting.name", maximum=128)
        if isinstance(self.value, str):
            require_text(self.value, "CommandSetting.value")


@dataclass(frozen=True, slots=True)
class LoadFlowRun:
    run_id: str
    configuration_key: ConfigurationKey
    calculation_input_digest: CalculationInputDigest
    command_settings: Tuple[CommandSetting, ...]
    engineering_policy: VersionedName
    convergence_state: ConvergenceState
    diagnostic_messages: Tuple[str, ...]
    result_snapshot_id: str
    duration: Quantity
    log_references: Tuple[str, ...]
    started_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        require_text(self.run_id, "LoadFlowRun.run_id", maximum=128)
        require_text(self.result_snapshot_id, "LoadFlowRun.result_snapshot_id", maximum=128)
        require_collection(self.command_settings, "LoadFlowRun.command_settings")
        require_collection(self.diagnostic_messages, "LoadFlowRun.diagnostic_messages")
        require_collection(self.log_references, "LoadFlowRun.log_references")
        for name, values in (
            ("diagnostic_messages", self.diagnostic_messages),
            ("log_references", self.log_references),
        ):
            for value in values:
                require_text(value, f"LoadFlowRun.{name} entry")
        if self.duration.unit != "s" or self.duration.value < 0:
            raise ValueError("LoadFlowRun.duration must be a nonnegative quantity in seconds")
        require_aware(self.started_at, "LoadFlowRun.started_at")
        require_aware(self.completed_at, "LoadFlowRun.completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("LoadFlowRun completed_at cannot precede started_at")


@dataclass(frozen=True, slots=True)
class Violation:
    violation_id: str
    asset: AssetReference
    violation_type: str
    measured_value: Quantity
    limit: Quantity
    severity: ViolationSeverity
    source_calculation_id: str
    trend: ViolationTrend

    def __post_init__(self) -> None:
        require_text(self.violation_id, "Violation.violation_id", maximum=128)
        require_text(self.violation_type, "Violation.violation_type", maximum=128)
        require_text(self.source_calculation_id, "Violation.source_calculation_id", maximum=128)
        if self.measured_value.unit != self.limit.unit:
            raise ValueError("Violation measured_value and limit must use the same unit")


@dataclass(frozen=True, slots=True)
class MetricDelta:
    asset: AssetReference
    metric: str
    before: Quantity
    after: Quantity
    delta: Quantity

    def __post_init__(self) -> None:
        require_text(self.metric, "MetricDelta.metric", maximum=128)
        if len({self.before.unit, self.after.unit, self.delta.unit}) != 1:
            raise ValueError("MetricDelta quantities must use the same unit")


@dataclass(frozen=True, slots=True)
class ResultComparison:
    comparison_id: str
    baseline_run_id: str
    candidate_run_id: str
    convergence_change: ConvergenceChange
    voltage_deltas: Tuple[MetricDelta, ...]
    loading_deltas: Tuple[MetricDelta, ...]
    added_violation_ids: Tuple[str, ...]
    removed_violation_ids: Tuple[str, ...]
    unchanged_violation_ids: Tuple[str, ...]
    material_changes: Tuple[MetricDelta, ...]
    materiality_policy: VersionedName

    def __post_init__(self) -> None:
        for field_name in ("comparison_id", "baseline_run_id", "candidate_run_id"):
            require_text(getattr(self, field_name), f"ResultComparison.{field_name}", maximum=128)
        for field_name in (
            "voltage_deltas",
            "loading_deltas",
            "added_violation_ids",
            "removed_violation_ids",
            "unchanged_violation_ids",
            "material_changes",
        ):
            values = getattr(self, field_name)
            require_collection(values, f"ResultComparison.{field_name}")
            if field_name.endswith("violation_ids"):
                for value in values:
                    require_text(value, f"ResultComparison.{field_name} entry", maximum=128)


@dataclass(frozen=True, slots=True)
class RollbackConflict:
    asset: AssetReference
    attribute: str
    expected_current: Quantity
    observed_current: Quantity
    reason: str

    def __post_init__(self) -> None:
        require_text(self.attribute, "RollbackConflict.attribute", maximum=256)
        require_text(self.reason, "RollbackConflict.reason")
        if self.expected_current.unit != self.observed_current.unit:
            raise ValueError("RollbackConflict quantities must use the same unit")


@dataclass(frozen=True, slots=True)
class RestorationValue:
    asset: AssetReference
    attribute: str
    current: Quantity
    restore_to: Quantity

    def __post_init__(self) -> None:
        require_text(self.attribute, "RestorationValue.attribute", maximum=256)
        if self.current.unit != self.restore_to.unit:
            raise ValueError("RestorationValue quantities must use the same unit")


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    rollback_plan_id: str
    workspace_id: str
    workflow_id: str
    applied_change_id: str
    conflicts: Tuple[RollbackConflict, ...]
    workspace_disposition: WorkspaceDisposition
    values_to_restore: Tuple[RestorationValue, ...]
    validation_steps: Tuple[str, ...]
    configuration_key: ConfigurationKey
    live_state_fingerprint: LiveStateFingerprint
    workspace_revision: WorkspaceRevision
    expected_workflow_version: WorkflowVersion
    expires_at: datetime
    content_digest: ContentDigest

    def __post_init__(self) -> None:
        from .values import require_uuid4

        require_text(self.rollback_plan_id, "RollbackPlan.rollback_plan_id", maximum=128)
        require_text(self.applied_change_id, "RollbackPlan.applied_change_id", maximum=128)
        require_collection(self.conflicts, "RollbackPlan.conflicts")
        require_collection(self.values_to_restore, "RollbackPlan.values_to_restore", allow_empty=False)
        require_collection(self.validation_steps, "RollbackPlan.validation_steps", allow_empty=False)
        for value in self.validation_steps:
            require_text(value, "RollbackPlan.validation_steps entry")
        require_aware(self.expires_at, "RollbackPlan.expires_at")
        require_uuid4(self.workspace_id, "RollbackPlan.workspace_id")
        require_uuid4(self.workflow_id, "RollbackPlan.workflow_id")
        if self.workspace_revision.scope_id != self.workspace_id:
            raise ValueError("workspace_revision scope must equal workspace_id")
        if self.expected_workflow_version.scope_id != self.workflow_id:
            raise ValueError("workflow version scope must equal workflow_id")


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    check: str
    passed: bool
    detail: str

    def __post_init__(self) -> None:
        require_text(self.check, "VerificationEvidence.check", maximum=256)
        require_text(self.detail, "VerificationEvidence.detail")


@dataclass(frozen=True, slots=True)
class RollbackResult:
    rollback_result_id: str
    workspace_id: str
    rollback_plan_id: str
    restored_values: Tuple[RestorationValue, ...]
    conflicts: Tuple[RollbackConflict, ...]
    verification_evidence: Tuple[VerificationEvidence, ...]
    baseline_reproduced: Optional[bool]
    completed_at: datetime
    verification_status: VerificationStatus
    workspace_revision: WorkspaceRevision

    def __post_init__(self) -> None:
        from .values import require_uuid4

        require_text(self.rollback_result_id, "RollbackResult.rollback_result_id", maximum=128)
        require_text(self.rollback_plan_id, "RollbackResult.rollback_plan_id", maximum=128)
        require_collection(self.restored_values, "RollbackResult.restored_values")
        require_collection(self.conflicts, "RollbackResult.conflicts")
        require_collection(self.verification_evidence, "RollbackResult.verification_evidence", allow_empty=False)
        require_aware(self.completed_at, "RollbackResult.completed_at")
        require_uuid4(self.workspace_id, "RollbackResult.workspace_id")
        if self.workspace_revision.scope_id != self.workspace_id:
            raise ValueError("workspace_revision scope must equal workspace_id")


@dataclass(frozen=True, slots=True)
class IdentityLifecycleRecord:
    product_identity: ProductIdentity
    state: IdentityLifecycleState
    observed_at: datetime
    evidence_reference: str

    def __post_init__(self) -> None:
        require_aware(self.observed_at, "IdentityLifecycleRecord.observed_at")
        require_text(self.evidence_reference, "IdentityLifecycleRecord.evidence_reference")


@dataclass(frozen=True, slots=True)
class LocatorRebind:
    product_identity: ProductIdentity
    prior_locator: PowerFactoryLocator
    replacement_locator: PowerFactoryLocator
    accepted_native_equality_evidence: bool
    evidence_reference: str

    def __post_init__(self) -> None:
        require_text(self.evidence_reference, "LocatorRebind.evidence_reference")
        if not self.accepted_native_equality_evidence:
            raise ValueError("locator rebind requires accepted native equality evidence")
        if self.prior_locator.object_class != self.replacement_locator.object_class:
            raise ValueError("locator rebind cannot change object class")
        if self.prior_locator.project_provenance != self.replacement_locator.project_provenance:
            raise ValueError("locator rebind cannot change project provenance")


@dataclass(frozen=True, slots=True)
class IdentityTombstone:
    product_identity: ProductIdentity
    evidence_revision: ExtractionRevision
    reason: str
    tombstoned_at: datetime
    complete_absence_evidence: bool

    def __post_init__(self) -> None:
        require_text(self.reason, "IdentityTombstone.reason")
        require_aware(self.tombstoned_at, "IdentityTombstone.tombstoned_at")
        if not self.complete_absence_evidence:
            raise ValueError("tombstoning requires complete absence evidence")


@dataclass(frozen=True, slots=True)
class PageCursor:
    """Opaque authenticated continuation token; protected fields stay server-side."""

    token: str

    def __post_init__(self) -> None:
        require_text(self.token, "PageCursor.token", maximum=4096)


@dataclass(frozen=True, slots=True)
class InventoryQuery:
    configuration_key: ConfigurationKey
    extraction_revision: ExtractionRevision
    asset_kind: Optional[AssetKind]
    exact_project_key: Optional[str]
    page_size: int
    cursor: Optional[PageCursor]
    sort_specification: str = "product_uuid_asc"

    def __post_init__(self) -> None:
        if self.exact_project_key is not None:
            require_text(self.exact_project_key, "InventoryQuery.exact_project_key")
        if isinstance(self.page_size, bool) or not isinstance(self.page_size, int) or not 1 <= self.page_size <= 100:
            raise ValueError("InventoryQuery.page_size must be between 1 and 100")
        if self.sort_specification != "product_uuid_asc":
            raise ValueError("only deterministic product_uuid_asc ordering is admitted")


@dataclass(frozen=True, slots=True)
class InventoryPage:
    query: InventoryQuery
    items: Tuple[AssetReference, ...]
    next_cursor: Optional[PageCursor]
    deterministic_order: bool = True

    def __post_init__(self) -> None:
        require_collection(self.items, "InventoryPage.items")
        if not self.deterministic_order:
            raise ValueError("inventory pages must declare deterministic ordering")
        identities = tuple(item.product_identity.value for item in self.items)
        if identities != tuple(sorted(identities)):
            raise ValueError("inventory items must be ordered by product UUID")
        if len(self.items) > self.query.page_size:
            raise ValueError("inventory page exceeds requested page size")
