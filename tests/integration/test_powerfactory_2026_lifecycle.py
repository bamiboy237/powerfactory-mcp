from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from powerfactory_agent.probes import (
    LifecycleProbeRunner,
    LifecycleStage,
    StageStatus,
    write_evidence_json,
)


class FakeLifecycleAdapter:
    def __init__(self, fail_run: int | None = None) -> None:
        self.fail_run = fail_run
        self.current_run = 0
        self.calls: list[tuple[int, LifecycleStage]] = []

    def execute_stage(self, stage: LifecycleStage) -> Mapping[str, Any]:
        if stage is LifecycleStage.ENVIRONMENT:
            self.current_run += 1
        self.calls.append((self.current_run, stage))

        if self.current_run == self.fail_run and stage is LifecycleStage.LOAD_FLOW:
            raise RuntimeError("injected load-flow failure password=must-not-leak")

        if stage is LifecycleStage.ENVIRONMENT:
            return {
                "architecture": "x86_64",
                "command_line": "profile=test token=must-not-leak",
                "license_type": "fake-training",
                "password": "must-not-leak",
                "release": "2026",
                "service_pack": "fake-sp",
            }
        return {"stage": stage.value, "run": self.current_run}


class LifecycleProbeTests(unittest.TestCase):
    def test_failed_run_is_cleaned_up_and_does_not_poison_next_run(self) -> None:
        adapter = FakeLifecycleAdapter(fail_run=1)

        evidence = LifecycleProbeRunner(adapter).run(repeat=2)

        self.assertEqual(evidence.runs[0].status, StageStatus.FAIL)
        self.assertEqual(evidence.runs[0].failure_stage, LifecycleStage.LOAD_FLOW)
        self.assertEqual(evidence.runs[1].status, StageStatus.PASS)
        failure = next(
            stage
            for stage in evidence.runs[0].stages
            if stage.stage is LifecycleStage.LOAD_FLOW
        )
        self.assertEqual(
            failure.error_message,
            "injected load-flow failure password=[REDACTED]",
        )
        self.assertEqual(
            [call for call in adapter.calls if call[1] is LifecycleStage.CLEANUP],
            [(1, LifecycleStage.CLEANUP), (2, LifecycleStage.CLEANUP)],
        )
        self.assertIn((2, LifecycleStage.LOAD_FLOW), adapter.calls)

    def test_sanitized_output_is_deterministic(self) -> None:
        first = LifecycleProbeRunner(FakeLifecycleAdapter()).run(repeat=2)
        second = LifecycleProbeRunner(FakeLifecycleAdapter()).run(repeat=2)

        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.json"
            second_path = Path(directory) / "second.json"
            write_evidence_json(first, first_path)
            write_evidence_json(second, second_path)

            self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
            document = json.loads(first_path.read_text(encoding="utf-8"))

        environment = document["runs"][0]["stages"][0]["data"]
        self.assertEqual(environment["password"], "[REDACTED]")
        self.assertEqual(
            environment["command_line"], "profile=test token=[REDACTED]"
        )
        self.assertEqual(environment["license_type"], "fake-training")

    def test_unsupported_evidence_fails_closed(self) -> None:
        class UnsafeAdapter(FakeLifecycleAdapter):
            def execute_stage(self, stage: LifecycleStage) -> Mapping[str, Any]:
                if stage is LifecycleStage.RESULTS:
                    return {"vendor_object": object()}
                return super().execute_stage(stage)

        evidence = LifecycleProbeRunner(UnsafeAdapter()).run()

        self.assertEqual(evidence.runs[0].failure_stage, LifecycleStage.RESULTS)
        result_stage = next(
            stage
            for stage in evidence.runs[0].stages
            if stage.stage is LifecycleStage.RESULTS
        )
        self.assertEqual(result_stage.error_type, "TypeError")
        self.assertEqual(
            result_stage.error_message, "unsupported evidence value: object"
        )


if __name__ == "__main__":
    unittest.main()
