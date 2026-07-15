"""Deterministic domain fixtures used by contract and unit tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from powerfactory_agent.domain import (
    AppliedChange, ApprovalRequest, AssetKind, AssetReference, AttributeQuantity,
    CalculationInputDigest, ChangePreview, CommandSetting, CompletenessState,
    ConfigurationKey, ConfirmedAssetChange, ContentDigest, ConvergenceChange,
    ConvergenceState, DependencyFingerprint, DependencySetIdentity, EngineeringOperation,
    ExecutionAuthorization, ExtractionRevision, FreshnessEvidence, FreshnessLevel,
    IdentityLifecycleState, LiveStateFingerprint, LoadFlowRun, LocatorEvidenceSchema,
    LocatorKind, LocatorTrust, MetricDelta, ModelContext, MutationStrategy, NamedValue,
    OperationType, PowerFactoryLocator, ProductIdentity, ProjectProvenance,
    ProposedAssetChange, Quantity, RestorationValue, ResultComparison, RollbackConflict,
    RollbackPlan, RollbackResult, VerificationEvidence, VerificationStatus, VersionedName,
    Violation, ViolationSeverity, ViolationTrend, WorkflowVersion, WorkspaceDisposition,
    WorkspaceRevision,
)


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
MODEL_CONTEXT_ID = "11111111-1111-4111-8111-111111111111"
WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
WORKFLOW_ID = "33333333-3333-4333-8333-333333333333"
CONFIGURATION = ConfigurationKey("configuration-key:v1:sha256:" + "0" * 64)
LIVE_FINGERPRINT = LiveStateFingerprint("live-state-fingerprint:v1:sha256:" + "1" * 64)
CONTENT_DIGEST = ContentDigest("content:v1:sha256:" + "2" * 64)
CALCULATION_DIGEST = CalculationInputDigest("calculation-input:v1:sha256:" + "3" * 64)
POLICY = VersionedName("engineering-policy", "1.0.0")
DEPENDENCY_SET = DependencySetIdentity("headline-loads", "v1")


def asset() -> AssetReference:
    return AssetReference(
        product_identity=ProductIdentity("12345678-1234-4234-9234-567812345678"),
        locator=PowerFactoryLocator(
            locator_version_id="44444444-4444-4444-8444-444444444444",
            locator_kind=LocatorKind.CANONICAL_PATH_FALLBACK,
            project_provenance=ProjectProvenance(
                "fixture-installation", "fixture-profile", "project-fixture", "fixture-project-evidence"
            ),
            object_class="ElmLod",
            native_field=None,
            native_value=None,
            canonical_path="Network/Load 1.ElmLod",
            evidence_schema=LocatorEvidenceSchema("powerfactory-locator", "v1", "fake-gateway/0.1.0"),
            observed_at=NOW,
            session_id="fixture-session",
            trust=LocatorTrust.FALLBACK,
        ),
        display_name="Load 1",
        asset_kind=AssetKind.LOAD,
        project_key="project-fixture",
        lifecycle_state=IdentityLifecycleState.ACTIVE,
    )


def proposed_change() -> ProposedAssetChange:
    return ProposedAssetChange(
        asset=asset(),
        before=(AttributeQuantity("plini", Quantity("10", "MW")),),
        proposed=(AttributeQuantity("plini", Quantity("11", "MW")),),
    )


def confirmed_change() -> ConfirmedAssetChange:
    return ConfirmedAssetChange(
        asset=asset(),
        before=(AttributeQuantity("plini", Quantity("10", "MW")),),
        proposed=(AttributeQuantity("plini", Quantity("11", "MW")),),
        confirmed=(AttributeQuantity("plini", Quantity("11", "MW")),),
    )


def preview() -> ChangePreview:
    return ChangePreview(
        preview_id="preview-1",
        model_context_id=MODEL_CONTEXT_ID,
        workspace_id=WORKSPACE_ID,
        workflow_id=WORKFLOW_ID,
        operation=EngineeringOperation(
            operation_type=OperationType.AREA_LOAD_SCALING,
            operation_specification=VersionedName("area-load-scaling", "1.0.0"),
            parameters=(NamedValue("area", "North"), AttributeQuantity("scale", Quantity("1.1", "ratio"))),
        ),
        resolved_changes=(proposed_change(),),
        selection_criteria=(NamedValue("area", "North"),),
        warnings=(), exclusions=("out-of-service loads",),
        configuration_key=CONFIGURATION, live_state_fingerprint=LIVE_FINGERPRINT,
        extraction_revision=ExtractionRevision(MODEL_CONTEXT_ID, 4),
        workspace_revision=WorkspaceRevision(WORKSPACE_ID, 2),
        expected_workflow_version=WorkflowVersion(WORKFLOW_ID, 6),
        engineering_policy=POLICY, expires_at=NOW + timedelta(minutes=5),
        content_digest=CONTENT_DIGEST, required_validation_steps=("validated load flow",),
    )


def all_primary_models() -> tuple[object, ...]:
    model_asset = asset()
    model_preview = preview()
    conflict = RollbackConflict(model_asset, "plini", Quantity("11", "MW"), Quantity("12", "MW"), "live value changed")
    restoration = RestorationValue(model_asset, "plini", Quantity("11", "MW"), Quantity("10", "MW"))
    delta = MetricDelta(model_asset, "voltage", Quantity("1", "p.u."), Quantity("0.99", "p.u."), Quantity("-0.01", "p.u."))
    freshness = FreshnessEvidence(
        FreshnessLevel.VERIFIED, NOW, "fixture-session", CONFIGURATION, DEPENDENCY_SET,
        "fixture:freshness", "fixture-policy", "v1", False,
    )
    dependency = DependencyFingerprint(
        DEPENDENCY_SET, LIVE_FINGERPRINT, CompletenessState.COMPLETE, NOW, "fixture-session",
        "fixture:fingerprint", VersionedName("fingerprint-policy", "v1"),
    )
    return (
        ModelContext(MODEL_CONTEXT_ID, CONFIGURATION, "2026", (model_asset,),
                     ExtractionRevision(MODEL_CONTEXT_ID, 4), NOW, freshness, (dependency,)),
        model_asset,
        model_preview,
        ApprovalRequest("approval-1", "preview-1", CONTENT_DIGEST, NOW, NOW + timedelta(minutes=5), "codex-agent", "mcp-client"),
        ExecutionAuthorization(
            "execution-1", WORKFLOW_ID, "approval-1", "local-user", CONTENT_DIGEST, CONFIGURATION,
            LIVE_FINGERPRINT, OperationType.AREA_LOAD_SCALING, MutationStrategy.DIRECT_LEDGER,
            WorkflowVersion(WORKFLOW_ID, 6), NOW, NOW + timedelta(minutes=2), "codex-agent", "mcp-client",
        ),
        AppliedChange("change-1", WORKSPACE_ID, "execution-1", CONTENT_DIGEST,
                      MutationStrategy.DIRECT_LEDGER, (confirmed_change(),), NOW, NOW + timedelta(seconds=1),
                      VerificationStatus.VERIFIED, WorkspaceRevision(WORKSPACE_ID, 3)),
        LoadFlowRun("run-1", CONFIGURATION, CALCULATION_DIGEST,
                    (CommandSetting("network_representation", "balanced"),), POLICY,
                    ConvergenceState.CONVERGED, (), "snapshot-1", Quantity("0.2", "s"),
                    ("logs/run-1.jsonl",), NOW, NOW + timedelta(seconds=1)),
        Violation("violation-1", model_asset, "undervoltage", Quantity("0.89", "p.u."),
                  Quantity("0.90", "p.u."), ViolationSeverity.CRITICAL, "run-1", ViolationTrend.NEW),
        ResultComparison("comparison-1", "run-0", "run-1", ConvergenceChange.UNCHANGED_CONVERGED,
                         (delta,), (), ("violation-1",), (), (), (delta,), VersionedName("materiality", "1.0.0")),
        RollbackPlan("rollback-plan-1", WORKSPACE_ID, WORKFLOW_ID, "change-1", (conflict,),
                     WorkspaceDisposition.RESTORE, (restoration,), ("validated load flow",), CONFIGURATION,
                     LIVE_FINGERPRINT, WorkspaceRevision(WORKSPACE_ID, 3), WorkflowVersion(WORKFLOW_ID, 8),
                     NOW + timedelta(minutes=5), ContentDigest("content:v1:sha256:" + "4" * 64)),
        RollbackResult("rollback-result-1", WORKSPACE_ID, "rollback-plan-1", (restoration,), (),
                       (VerificationEvidence("load flow equivalence", True, "within tolerance"),), True,
                       NOW + timedelta(minutes=1), VerificationStatus.VERIFIED, WorkspaceRevision(WORKSPACE_ID, 4)),
    )
