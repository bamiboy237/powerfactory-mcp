from __future__ import annotations

import json
from pathlib import Path
import unittest

from powerfactory_agent.domain import Quantity
from powerfactory_agent.serialization import (
    SerializationError,
    canonical_digest,
    canonical_json,
    from_json,
    to_primitive,
)

from .fixtures import all_primary_models, preview


class SerializationTests(unittest.TestCase):
    def test_all_primary_models_round_trip_with_one_encoding_layer(self) -> None:
        for model in all_primary_models():
            with self.subTest(model=type(model).__name__):
                encoded = canonical_json(model)
                self.assertIsInstance(json.loads(encoded), dict)
                self.assertEqual(model, from_json(type(model), encoded))

    def test_canonical_output_and_digest_are_order_independent(self) -> None:
        left = {"z": 1, "a": (Quantity("1.00", "MW"),)}
        right = {"a": (Quantity("1.00", "MW"),), "z": 1}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(canonical_digest(left), canonical_digest(right))
        self.assertRegex(canonical_digest(left), r"^content:v1:sha256:[0-9a-f]{64}$")

    def test_canonical_unicode_decimal_timestamp_and_golden_bytes(self) -> None:
        golden = Path(__file__).with_name("golden") / "canonical-json-v1.json"
        expected = golden.read_bytes().rstrip(b"\n")
        value = {
            "timestamp": all_primary_models()[0].extracted_at,
            "quantity": Quantity("10.5000", "MW"),
            "label": "Cafe\u0301",
        }
        actual = canonical_json(value).encode("utf-8")
        self.assertEqual(expected, actual)
        expected_digest = (golden.parent / "canonical-json-v1.digest.txt").read_text().strip()
        self.assertEqual(expected_digest, canonical_digest(value, kind="golden-fixture", schema="v1"))

    def test_nfc_key_collisions_are_rejected(self) -> None:
        with self.assertRaises(SerializationError):
            canonical_json({"Caf\u00e9": 1, "Cafe\u0301": 2})

    def test_serializer_rejects_raw_handles_non_string_keys_and_nonfinite_values(self) -> None:
        class RawPowerFactoryHandle:
            pass

        for value in (RawPowerFactoryHandle(), {1: "bad"}, float("nan")):
            with self.subTest(value=type(value).__name__), self.assertRaises(SerializationError):
                to_primitive(value)

    def test_decoder_rejects_extra_fields_and_nonfinite_constants(self) -> None:
        primitive = to_primitive(preview())
        assert isinstance(primitive, dict)
        primitive["raw_handle"] = "not admitted"
        with self.assertRaises(SerializationError):
            from_json(type(preview()), json.dumps(primitive))
        with self.assertRaises(SerializationError):
            from_json(Quantity, '{"unit":"MW","value":NaN}')

    def test_payload_size_limit_fails_closed(self) -> None:
        with self.assertRaises(SerializationError):
            canonical_json(preview(), maximum_bytes=10)


if __name__ == "__main__":
    unittest.main()
