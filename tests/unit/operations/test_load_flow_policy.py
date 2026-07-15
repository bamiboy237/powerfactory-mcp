from __future__ import annotations

from decimal import Decimal
import unittest

from powerfactory_agent.domain import (
    EvaluationStatus,
    MetricDefinition,
    MetricKind,
    ResultCellStatus,
    ResultMetric,
    VersionedName,
)
from powerfactory_agent.operations import evaluate_metric
from powerfactory_agent.domain.values import ProductIdentity, Quantity
from powerfactory_agent.gateway import DeterministicPrimitiveGateway


class LoadFlowPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = DeterministicPrimitiveGateway()
        self.voltage = self._definition(
            "voltage-north",
            MetricKind.BUS_VOLTAGE,
            self.gateway.bus,
            self.gateway.bus_voltage_result,
            "p.u.",
            Quantity("0.95", "p.u."),
            Quantity("1.05", "p.u."),
            Quantity("0.02", "p.u."),
        )
        self.loading = self._definition(
            "loading-north",
            MetricKind.EQUIPMENT_LOADING,
            self.gateway.load,
            self.gateway.equipment_loading_result,
            "%",
            None,
            Quantity("100", "%"),
            Quantity("10", "%"),
        )

    def test_inclusive_voltage_and_loading_limits_are_safe(self) -> None:
        for definition, value in (
            (self.voltage, Quantity("0.95", "p.u.")),
            (self.voltage, Quantity("1.05", "p.u.")),
            (self.loading, Quantity("100", "%")),
        ):
            evaluation = evaluate_metric(self._available(definition, value))
            self.assertEqual(EvaluationStatus.SAFE, evaluation.status)
            self.assertIsNone(evaluation.violation)

    def test_voltage_and_loading_excesses_have_explicit_direction_and_severity(self) -> None:
        lower = evaluate_metric(self._available(self.voltage, Quantity("0.94", "p.u.")))
        upper = evaluate_metric(self._available(self.voltage, Quantity("1.08", "p.u.")))
        loading = evaluate_metric(self._available(self.loading, Quantity("111", "%")))
        self.assertEqual(EvaluationStatus.VIOLATION, lower.status)
        self.assertEqual("lower", lower.violation.direction)
        self.assertEqual("warning", lower.violation.severity.value)
        self.assertEqual("upper", upper.violation.direction)
        self.assertEqual("critical", upper.violation.severity.value)
        self.assertEqual("upper", loading.violation.direction)
        self.assertEqual("critical", loading.violation.severity.value)

    def test_missing_limit_never_claims_safety(self) -> None:
        no_limits = self._definition(
            "unlimited-voltage",
            MetricKind.BUS_VOLTAGE,
            self.gateway.bus,
            self.gateway.bus_voltage_result,
            "p.u.",
            None,
            None,
            Quantity("0.01", "p.u."),
        )
        result = evaluate_metric(self._available(no_limits, Quantity("1.0", "p.u.")))
        self.assertEqual(EvaluationStatus.NOT_EVALUATED_MISSING_LIMIT, result.status)

    def test_missing_unsupported_and_non_finite_data_are_not_evaluated(self) -> None:
        for status in (ResultCellStatus.MISSING, ResultCellStatus.UNSUPPORTED, ResultCellStatus.NON_FINITE):
            metric = (
                ResultMetric(self.voltage, status, "nan", "p.u.", None, "fixture")
                if status is ResultCellStatus.NON_FINITE
                else ResultMetric(self.voltage, status, None, None, None, "fixture")
            )
            result = evaluate_metric(metric)
            self.assertEqual(EvaluationStatus.NOT_EVALUATED_DATA, result.status)
            self.assertIsNone(result.violation)

    @staticmethod
    def _definition(definition_id, metric_kind, selector, variable, unit, lower, upper, critical):
        return MetricDefinition(
            definition_id,
            ProductIdentity(
                "10000000-0000-4000-8000-000000000001"
                if metric_kind is MetricKind.BUS_VOLTAGE
                else "20000000-0000-4000-8000-000000000002"
            ),
            metric_kind,
            selector,
            variable,
            unit,
            lower,
            upper,
            critical,
            Quantity("0.001" if unit == "p.u." else "0.1", unit),
            Quantity("0.01" if unit == "p.u." else "1", unit),
            "fixture limits/v1",
        )

    @staticmethod
    def _available(definition, value):
        return ResultMetric(definition, ResultCellStatus.AVAILABLE, str(value.value), value.unit, value, None)


if __name__ == "__main__":
    unittest.main()
