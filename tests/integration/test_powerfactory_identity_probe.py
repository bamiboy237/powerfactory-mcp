from __future__ import annotations

import unittest
from typing import Any, Mapping

from powerfactory_agent.probes import (
    IdentityObservation,
    IdentityOperation,
    LifecycleProbeRunner,
    LifecycleStage,
)


class FakeIdentityAdapter:
    def execute_stage(self, stage: LifecycleStage) -> Mapping[str, Any]:
        if stage is not LifecycleStage.IDENTITY:
            return {"stage": stage.value}
        return {
            "observations": [
                IdentityObservation(
                    IdentityOperation.DUPLICATE_NAME,
                    "duplicate-a",
                    "native-a",
                    "native-a",
                    related_identity="native-b",
                ),
                IdentityObservation(
                    IdentityOperation.RENAME,
                    "asset-a",
                    "native-a",
                    "native-a",
                    before_locator="Grid/Old Name.ElmLod",
                    after_locator="Grid/New Name.ElmLod",
                ),
                IdentityObservation(
                    IdentityOperation.MOVE,
                    "asset-a",
                    "native-a",
                    "native-a",
                    before_locator="Grid/Folder A/New Name.ElmLod",
                    after_locator="Grid/Folder B/New Name.ElmLod",
                ),
                IdentityObservation(
                    IdentityOperation.COPY,
                    "asset-a-copy",
                    "native-a",
                    "native-copy",
                    related_identity="native-a",
                ),
                IdentityObservation(
                    IdentityOperation.DELETE,
                    "asset-a",
                    "native-a",
                    None,
                    exists_after=False,
                ),
                IdentityObservation(
                    IdentityOperation.RECREATE_SAME_NAME,
                    "asset-a-recreated",
                    "native-a",
                    "native-recreated",
                ),
            ]
        }


class IdentityProbeTests(unittest.TestCase):
    def test_identity_observations_cover_required_transitions(self) -> None:
        evidence = LifecycleProbeRunner(FakeIdentityAdapter()).run()
        identity_stage = next(
            stage
            for stage in evidence.runs[0].stages
            if stage.stage is LifecycleStage.IDENTITY
        )
        observations = {
            item["operation"]: item for item in identity_stage.data["observations"]
        }

        self.assertEqual(set(observations), {operation.value for operation in IdentityOperation})
        for operation, observation in observations.items():
            self.assertEqual(observation["operation"], operation)
            self.assertIn("before_identity", observation)
            self.assertIn("after_identity", observation)
            self.assertIn("exists_after", observation)


if __name__ == "__main__":
    unittest.main()
