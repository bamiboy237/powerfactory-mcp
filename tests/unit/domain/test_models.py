from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
from decimal import Decimal
import unittest

from powerfactory_agent.domain import (
    AssetReference,
    CompletenessState,
    ConfigurationKey,
    ExtractionRevision,
    LiveStateFingerprint,
    LocatorTrust,
    PowerFactoryLocator,
    ProductIdentity,
    Quantity,
    WorkflowVersion,
    WorkspaceRevision,
)

from .fixtures import MODEL_CONTEXT_ID, WORKSPACE_ID, all_primary_models, asset, proposed_change


class DomainModelTests(unittest.TestCase):
    def test_all_required_primary_contracts_are_immutable(self) -> None:
        models = all_primary_models()
        self.assertEqual(11, len(models))
        for model in models:
            with self.subTest(model=type(model).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(model, next(iter(model.__dataclass_fields__)), object())

    def test_six_state_identities_have_distinct_types(self) -> None:
        identity_types = {
            ConfigurationKey,
            LiveStateFingerprint,
            ExtractionRevision,
            WorkspaceRevision,
            WorkflowVersion,
        }
        # CalculationInputDigest is exercised separately in LoadFlowRun fixtures.
        from powerfactory_agent.domain import CalculationInputDigest

        identity_types.add(CalculationInputDigest)
        self.assertEqual(6, len(identity_types))

    def test_product_identity_is_opaque_uuid_and_separate_from_locator(self) -> None:
        reference = asset()
        self.assertIsInstance(reference.product_identity, ProductIdentity)
        self.assertIsInstance(reference.locator, PowerFactoryLocator)
        self.assertNotEqual(reference.product_identity.value, reference.locator.canonical_path)
        with self.assertRaises(ValueError):
            ProductIdentity("Network/Load 1.ElmLod")
        with self.assertRaises(ValueError):
            ProductIdentity("12345678-1234-5234-9234-567812345678")
        with self.assertRaises(ValueError):
            ProductIdentity("12345678-1234-4234-9234-56781234567A")

    def test_quantity_requires_finite_value_and_explicit_unit(self) -> None:
        for value in (float("nan"), float("inf"), -float("inf"), 1.0):
            with self.subTest(value=value), self.assertRaises(TypeError):
                Quantity(value, "MW")
        with self.assertRaises(ValueError):
            Quantity(Decimal("NaN"), "MW")
        with self.assertRaises(ValueError):
            Quantity("1", "")

    def test_revision_scope_prevents_cross_context_equality_and_binding(self) -> None:
        other_scope = "99999999-9999-4999-8999-999999999999"
        self.assertNotEqual(ExtractionRevision(MODEL_CONTEXT_ID, 4), ExtractionRevision(other_scope, 4))
        self.assertEqual(
            "workspace-revision/v1:22222222-2222-4222-8222-222222222222:2",
            WorkspaceRevision(WORKSPACE_ID, 2).wire,
        )
        self.assertEqual(
            WorkspaceRevision(WORKSPACE_ID, 2),
            WorkspaceRevision.from_wire("workspace-revision/v1:22222222-2222-4222-8222-222222222222:2"),
        )
        context = all_primary_models()[0]
        values = {name: getattr(context, name) for name in context.__dataclass_fields__}
        values["extraction_revision"] = ExtractionRevision(other_scope, 4)
        with self.assertRaises(ValueError):
            type(context)(**values)

    def test_verified_context_rejects_empty_or_unsupported_dependency_evidence(self) -> None:
        context = all_primary_models()[0]
        values = {name: getattr(context, name) for name in context.__dataclass_fields__}
        values["dependency_fingerprints"] = ()
        with self.assertRaises(ValueError):
            type(context)(**values)
        dependency = context.dependency_fingerprints[0]
        dependency_values = {name: getattr(dependency, name) for name in dependency.__dataclass_fields__}
        dependency_values["completeness"] = CompletenessState.UNSUPPORTED
        values["dependency_fingerprints"] = (type(dependency)(**dependency_values),)
        with self.assertRaises(ValueError):
            type(context)(**values)

    def test_provisional_locator_cannot_claim_verified_native(self) -> None:
        locator = asset().locator
        values = {name: getattr(locator, name) for name in locator.__dataclass_fields__}
        values["trust"] = LocatorTrust.VERIFIED_NATIVE
        values["native_evidence_accepted"] = False
        with self.assertRaises(ValueError):
            type(locator)(**values)

    def test_timestamps_must_be_timezone_aware(self) -> None:
        context = all_primary_models()[0]
        values = dict((name, getattr(context, name)) for name in context.__dataclass_fields__)
        values["extracted_at"] = datetime(2026, 7, 14, 12, 0)
        with self.assertRaises(ValueError):
            type(context)(**values)

    def test_collections_are_immutable_and_bounded_by_construction(self) -> None:
        change = proposed_change()
        with self.assertRaises(TypeError):
            type(change)(asset=change.asset, before=list(change.before), proposed=change.proposed)  # type: ignore[arg-type]

    def test_asset_project_must_match_locator_project(self) -> None:
        reference = asset()
        with self.assertRaises(ValueError):
            AssetReference(
                product_identity=reference.product_identity,
                locator=reference.locator,
                display_name=reference.display_name,
                asset_kind=reference.asset_kind,
                project_key="different-project",
                lifecycle_state=reference.lifecycle_state,
            )


if __name__ == "__main__":
    unittest.main()
