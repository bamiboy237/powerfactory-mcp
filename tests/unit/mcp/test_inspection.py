from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from powerfactory_agent.mcp.configuration import configure_probe, create_installation
from powerfactory_agent.mcp.inspection import run_active_project_inspection
from powerfactory_agent.probes import LifecycleStage


class StubInspectionAdapter:
    def __init__(self, *, fail_at: LifecycleStage | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[LifecycleStage] = []

    def execute_stage(self, stage: LifecycleStage) -> dict[str, object]:
        self.calls.append(stage)
        if stage is self.fail_at:
            raise RuntimeError("inspection failed token=must-not-leak")
        if stage is LifecycleStage.ACTIVATE_PROJECT:
            return {"active_project": {"name": "Project A", "class_name": "IntPrj"}}
        if stage is LifecycleStage.ACTIVATE_STUDY_CASE:
            return {"active_study_case": {"name": "Case A", "class_name": "IntCase"}}
        if stage is LifecycleStage.INVENTORY:
            return {
                "classes": {
                    "ElmLod": {"count": 2, "sample": [{"name": "Load A"}]},
                    "ElmLne": {"count": 1, "sample": [{"name": "Line A"}]},
                },
                "sample_limit_per_class": 1,
            }
        return {"stage": stage.value}


class ActiveProjectInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        state_dir = Path(self.directory.name) / "agent"
        self.installation = create_installation(state_dir)
        pyd_path = Path(self.directory.name) / "powerfactory.pyd"
        pyd_path.touch()
        configure_probe(
            state_dir / "powerfactory-agent.json",
            {
                "pyd_path": str(pyd_path),
                "python_version": "3.12",
                "project_selector": "@active",
                "study_case": "@active",
                "sample_limit": 1,
                "cardinality_ceiling": 10,
                "include_out_of_service": False,
                "session_ownership": "attached",
            },
        )
        from powerfactory_agent.mcp.configuration import load_installation

        self.installation = load_installation(state_dir / "powerfactory-agent.json")

    def test_inspection_stops_before_calculation_and_persists_bounded_evidence(self) -> None:
        adapter = StubInspectionAdapter()

        result = run_active_project_inspection(
            self.installation,
            adapter_factory=lambda _: adapter,
        )

        self.assertEqual("PASS", result["status"])
        self.assertTrue(result["read_only"])
        self.assertFalse(result["calculation_executed"])
        self.assertNotIn(LifecycleStage.LOAD_FLOW, adapter.calls)
        self.assertEqual(LifecycleStage.CLEANUP, adapter.calls[-1])
        self.assertEqual(2, result["inventory"]["classes"]["ElmLod"]["count"])
        evidence_path = self.installation.log_file.parent / "evidence" / result["evidence_file"]
        self.assertEqual("PASS", json.loads(evidence_path.read_text())["status"])

    def test_failure_is_structured_redacted_and_cleanup_still_runs(self) -> None:
        adapter = StubInspectionAdapter(fail_at=LifecycleStage.INVENTORY)

        result = run_active_project_inspection(
            self.installation,
            adapter_factory=lambda _: adapter,
        )

        self.assertEqual("FAIL", result["status"])
        self.assertEqual("inventory", result["failure_stage"])
        self.assertIn("[REDACTED]", result["failure"]["message"])
        self.assertEqual(LifecycleStage.CLEANUP, adapter.calls[-1])

    def test_non_active_selectors_are_rejected_before_adapter_creation(self) -> None:
        config_path = self.installation.log_file.parent / "powerfactory-agent.json"
        configure_probe(
            config_path,
            {
                "pyd_path": str(Path(self.directory.name) / "powerfactory.pyd"),
                "python_version": "3.12",
                "project_selector": "Project A",
                "study_case": "Case A",
                "sample_limit": 1,
                "cardinality_ceiling": 10,
                "include_out_of_service": False,
                "session_ownership": "attached",
            },
        )
        from powerfactory_agent.mcp.configuration import load_installation

        exact_installation = load_installation(config_path)
        with self.assertRaisesRegex(ValueError, "requires project and study case selectors"):
            run_active_project_inspection(
                exact_installation,
                adapter_factory=lambda _: self.fail("adapter must not be created"),
            )
