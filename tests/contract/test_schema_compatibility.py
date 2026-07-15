from __future__ import annotations

import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from powerfactory_agent.domain import AssetKind, AssetLookupRequest, ComponentListRequest, ModelSummaryRequest
from powerfactory_agent.domain.schema import DOMAIN_TYPES, SCHEMA_PATH, check_schema, generate_domain_schema
from powerfactory_agent.gateway.fake import DeterministicFakeGateway
from powerfactory_agent.operations import InventoryService
from powerfactory_agent.serialization import canonical_json, from_json, to_primitive
from tests.unit.domain.fixtures import all_primary_models


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def inventory_models() -> tuple[object, ...]:
    gateway = DeterministicFakeGateway()
    service = InventoryService(gateway)
    project_key = "project-fixture"
    return (
        service.summarize(ModelSummaryRequest(project_key)),
        service.list_components(ComponentListRequest(AssetKind.LOAD, project_key, 1)),
        service.lookup(
            AssetLookupRequest(gateway.load_1.product_identity, AssetKind.LOAD, "ElmLod", project_key)
        ),
    )


class DomainSchemaCompatibilityTests(unittest.TestCase):
    def test_checked_in_schema_exactly_matches_generator(self) -> None:
        self.assertTrue(check_schema(REPOSITORY_ROOT), f"run generator for {SCHEMA_PATH}")

    def test_schema_is_versioned_closed_and_covers_all_public_contract_types(self) -> None:
        schema = generate_domain_schema()
        self.assertEqual("0.3.0", schema["x-schema-version"])
        definitions = schema["$defs"]
        self.assertEqual({item.__name__ for item in DOMAIN_TYPES}, set(definitions))
        for name, definition in definitions.items():
            with self.subTest(name=name):
                if definition.get("type") == "object":
                    self.assertFalse(definition["additionalProperties"])

    def test_primary_contracts_round_trip_through_schema_shaped_json(self) -> None:
        definitions = generate_domain_schema()["$defs"]
        for model in all_primary_models() + inventory_models():
            with self.subTest(model=type(model).__name__):
                self.assertIn(type(model).__name__, definitions)
                payload = canonical_json(model)
                self.assertEqual(model, from_json(type(model), payload))
                self.assertIsInstance(json.loads(payload), dict)

    def test_draft_2020_12_validator_accepts_positive_model_fixtures(self) -> None:
        schema = generate_domain_schema()
        Draft202012Validator.check_schema(schema)
        for model in all_primary_models() + inventory_models():
            with self.subTest(model=type(model).__name__):
                validator = Draft202012Validator(
                    {
                        "$schema": schema["$schema"],
                        "$ref": f"#/$defs/{type(model).__name__}",
                        "$defs": schema["$defs"],
                    }
                )
                validator.validate(to_primitive(model))

    def test_draft_2020_12_validator_rejects_negative_wire_fixtures(self) -> None:
        schema = generate_domain_schema()
        cases = (
            ("Quantity", {"value": 1.25, "unit": "MW"}),
            ("ProductIdentity", {"value": "12345678-1234-5234-9234-567812345678"}),
            ("ExtractionRevision", {"scope_id": "11111111-1111-4111-8111-111111111111", "counter": -1}),
            ("ContentDigest", {"value": "f" * 64}),
            ("Quantity", {"value": "1", "unit": "MW", "raw_handle": "forbidden"}),
        )
        for definition_name, instance in cases:
            with self.subTest(definition=definition_name, instance=instance):
                validator = Draft202012Validator(
                    {
                        "$schema": schema["$schema"],
                        "$ref": f"#/$defs/{definition_name}",
                        "$defs": schema["$defs"],
                    }
                )
                self.assertFalse(validator.is_valid(instance))


if __name__ == "__main__":
    unittest.main()
