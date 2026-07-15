from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, fields
import inspect
import unittest

import powerfactory_agent.config.models as config_models
import powerfactory_agent.config.settings as config_settings
from powerfactory_agent.config import (
    DEVELOPMENT_DEFAULTS_PROFILE,
    ENVIRONMENT_PREFIX,
    AgentSettings,
    ByteCount,
    EntryCount,
    PercentagePointDelta,
    PerUnitDelta,
    Seconds,
    load_settings,
    load_settings_from_environment,
)


class ConfigurationDefaultsTests(unittest.TestCase):
    def test_config_modules_do_not_import_powerfactory(self) -> None:
        imported_roots: set[str] = set()
        for module in (config_models, config_settings):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_roots.add(node.module.split(".", 1)[0])
        self.assertNotIn("powerfactory", imported_roots)

    def test_defaults_are_named_immutable_and_development_only(self) -> None:
        settings = AgentSettings()
        self.assertEqual("development-unvalidated-windows/v1", DEVELOPMENT_DEFAULTS_PROFILE)
        self.assertEqual(DEVELOPMENT_DEFAULTS_PROFILE, settings.DEFAULTS_PROFILE)
        self.assertEqual(300.0, settings.cached_freshness_max_age.value)
        self.assertEqual(30.0, settings.verified_freshness_max_age.value)
        self.assertEqual(100, settings.inventory_default_page_size.value)
        self.assertEqual(1_000, settings.inventory_max_page_size.value)
        self.assertEqual(10_000, settings.maximum_collection_entries.value)
        self.assertEqual(1_048_576, settings.maximum_serialized_payload.value)
        self.assertEqual(0.001, settings.voltage_materiality.value)
        self.assertEqual(1.0, settings.loading_materiality.value)
        self.assertEqual(30.0, settings.queue_wait_timeout.value)
        self.assertEqual(120.0, settings.client_response_timeout.value)
        self.assertEqual(300.0, settings.engine_call_timeout.value)
        self.assertEqual(60.0, settings.startup_timeout.value)
        self.assertEqual(30.0, settings.shutdown_timeout.value)
        self.assertEqual(250, settings.fake_gateway_cardinality.value)
        with self.assertRaises(FrozenInstanceError):
            settings.cached_freshness_max_age = Seconds(1)  # type: ignore[misc]

    def test_every_field_uses_a_unit_or_count_type(self) -> None:
        settings = AgentSettings()
        allowed_types = (Seconds, EntryCount, ByteCount, PerUnitDelta, PercentagePointDelta)
        for item in fields(settings):
            with self.subTest(field=item.name):
                self.assertIsInstance(getattr(settings, item.name), allowed_types)

    def test_value_types_reject_invalid_values_and_booleans(self) -> None:
        invalid = (
            (Seconds, 0),
            (Seconds, float("inf")),
            (EntryCount, -1),
            (ByteCount, 0),
            (PerUnitDelta, 1.01),
            (PercentagePointDelta, 101),
        )
        for value_type, value in invalid:
            with self.subTest(value_type=value_type.__name__, value=value):
                with self.assertRaises(ValueError):
                    value_type(value)
        for value_type in (Seconds, EntryCount, ByteCount, PerUnitDelta, PercentagePointDelta):
            with self.subTest(value_type=value_type.__name__), self.assertRaises(TypeError):
                value_type(True)

    def test_cross_field_ranges_fail_closed(self) -> None:
        invalid_overrides = (
            {"verified_freshness_max_age_seconds": 301},
            {"inventory_default_page_size_entries": 1001},
            {"inventory_max_page_size_entries": 10001},
            {"fake_gateway_cardinality_entries": 10001},
            {"client_response_timeout_seconds": 301},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                load_settings(overrides)


class ConfigurationLoaderTests(unittest.TestCase):
    def test_mapping_overrides_are_deterministic_and_leave_defaults_unchanged(self) -> None:
        first = load_settings(
            {
                "inventory_default_page_size_entries": "25",
                "voltage_materiality_per_unit": "0.0025",
            }
        )
        second = load_settings(
            {
                "voltage_materiality_per_unit": 0.0025,
                "inventory_default_page_size_entries": 25,
            }
        )
        self.assertEqual(first, second)
        self.assertEqual(25, first.inventory_default_page_size.value)
        self.assertEqual(0.0025, first.voltage_materiality.value)
        self.assertEqual(AgentSettings(), load_settings())

    def test_unknown_mapping_key_fails_without_echoing_value(self) -> None:
        secret = "not-for-error-output"
        with self.assertRaisesRegex(ValueError, "unknown configuration keys: password") as raised:
            load_settings({"password": secret})
        self.assertNotIn(secret, str(raised.exception))

    def test_unknown_prefixed_environment_key_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown configuration keys: surprise"):
            load_settings_from_environment({f"{ENVIRONMENT_PREFIX}SURPRISE": "1"})

    def test_environment_loader_ignores_unrelated_keys_and_applies_prefix(self) -> None:
        settings = load_settings_from_environment(
            {
                "PATH": "/irrelevant",
                f"{ENVIRONMENT_PREFIX}QUEUE_WAIT_TIMEOUT_SECONDS": "12.5",
                f"{ENVIRONMENT_PREFIX}MAXIMUM_SERIALIZED_PAYLOAD_BYTES": "2048",
            }
        )
        self.assertEqual(12.5, settings.queue_wait_timeout.value)
        self.assertEqual(2048, settings.maximum_serialized_payload.value)

    def test_boolean_and_ambiguous_numeric_overrides_fail(self) -> None:
        invalid = (
            {"inventory_default_page_size_entries": True},
            {"inventory_default_page_size_entries": "01"},
            {"inventory_default_page_size_entries": "1.0"},
            {"queue_wait_timeout_seconds": False},
            {"queue_wait_timeout_seconds": " 1"},
            {"queue_wait_timeout_seconds": "nan"},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises((TypeError, ValueError)):
                load_settings(overrides)

    def test_mapping_shape_and_value_types_are_strict(self) -> None:
        with self.assertRaises(TypeError):
            load_settings([])  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            load_settings({1: "value"})  # type: ignore[dict-item]
        with self.assertRaises(TypeError):
            load_settings({"queue_wait_timeout_seconds": object()})

    def test_repr_contains_no_secret_fields(self) -> None:
        rendered = repr(AgentSettings()).lower()
        self.assertNotIn("password", rendered)
        self.assertNotIn("token", rendered)
        self.assertNotIn("secret", rendered)


if __name__ == "__main__":
    unittest.main()
