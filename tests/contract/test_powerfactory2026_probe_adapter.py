from __future__ import annotations

import math
import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

from powerfactory_agent.probes import (
    LifecycleProbeRunner,
    LifecycleStage,
    PowerFactory2026LifecycleAdapter,
    PowerFactory2026ProbeConfig,
    SessionOwnership,
    StageStatus,
)


class FakeObject:
    def __init__(
        self,
        name: str,
        class_name: str,
        full_name: str,
        *,
        attributes: Mapping[str, Any] | None = None,
        units: Mapping[str, str] | None = None,
        on_activate: Callable[[FakeObject], None] | None = None,
        on_deactivate: Callable[[FakeObject], None] | None = None,
        activate_return_code: int = 0,
    ) -> None:
        self.loc_name = name
        self.class_name = class_name
        self.full_name = full_name
        self.attributes = dict(attributes or {})
        self.units = dict(units or {})
        self.on_activate = on_activate
        self.on_deactivate = on_deactivate
        self.activate_return_code = activate_return_code
        self.activation_count = 0
        self.deactivation_count = 0

    def GetFullName(self) -> str:
        return self.full_name

    def GetClassName(self) -> str:
        return self.class_name

    def GetAttribute(self, name: str) -> Any:
        return self.attributes[name]

    def GetAttributeUnit(self, name: str) -> str:
        return self.units[name]

    def Activate(self) -> int:
        self.activation_count += 1
        if self.activate_return_code == 0 and self.on_activate is not None:
            self.on_activate(self)
        return self.activate_return_code

    def Deactivate(self) -> int:
        self.deactivation_count += 1
        if self.on_deactivate is not None:
            self.on_deactivate(self)
        return 0


class FakeFolder:
    def __init__(self, objects: list[FakeObject]) -> None:
        self.objects = objects
        self.calls: list[tuple[str, int]] = []

    def GetContents(self, query: str, recursive: int) -> list[FakeObject]:
        self.calls.append((query, recursive))
        return list(self.objects)


class FakeCommand(FakeObject):
    def __init__(self, return_code: int = 0) -> None:
        super().__init__("Load Flow", "ComLdf", "Study/Load Flow.ComLdf")
        self.return_code = return_code
        self.execute_count = 0

    def Execute(self) -> int:
        self.execute_count += 1
        return self.return_code


class FakeApplication:
    def __init__(
        self,
        *,
        project_names: tuple[str, ...] = ("Project A",),
        study_case_names: tuple[str, ...] = ("Case A",),
        inventories: Mapping[str, list[FakeObject]] | None = None,
        load_flow_return_code: int = 0,
        project_activation_return_code: int = 0,
        study_case_activation_return_code: int = 0,
    ) -> None:
        self.active_project: FakeObject | None = None
        self.active_study_case: FakeObject | None = None
        self.projects = [
            FakeObject(
                name,
                "IntPrj",
                f"Projects/{index}/{name}.IntPrj",
                on_activate=self._set_active_project,
                on_deactivate=self._clear_active_project,
                activate_return_code=project_activation_return_code,
            )
            for index, name in enumerate(project_names)
        ]
        self.study_cases = [
            FakeObject(
                name,
                "IntCase",
                f"Study/{index}/{name}.IntCase",
                on_activate=self._set_active_study_case,
                on_deactivate=self._clear_active_study_case,
                activate_return_code=study_case_activation_return_code,
            )
            for index, name in enumerate(study_case_names)
        ]
        self.current_user = FakeFolder(self.projects)
        self.folders = {
            "study": FakeFolder(self.study_cases),
        }
        self.inventories = dict(inventories or _default_inventories())
        self.command = FakeCommand(load_flow_return_code)
        self.activate_project_calls: list[str] = []
        self.inventory_calls: list[tuple[str, bool]] = []
        self.post_commands: list[str] = []
        self.deactivate_project_count = 0
        self.show_count = 0

    def _set_active_study_case(self, value: FakeObject) -> None:
        self.active_study_case = value

    def _clear_active_study_case(self, value: FakeObject) -> None:
        if self.active_study_case is value:
            self.active_study_case = None

    def _set_active_project(self, value: FakeObject) -> None:
        self.active_project = value
        self.active_study_case = None

    def _clear_active_project(self, value: FakeObject) -> None:
        if self.active_project is value:
            self.active_project = None

    def GetCurrentUser(self) -> FakeFolder:
        return self.current_user

    def GetProjectFolder(self, folder_name: str) -> FakeFolder:
        return self.folders[folder_name]

    def ActivateProject(self, selector: str) -> int:
        self.activate_project_calls.append(selector)
        matches = [
            project
            for project in self.projects
            if selector in {project.loc_name, project.GetFullName()}
        ]
        if len(matches) != 1:
            return 1
        return matches[0].Activate()

    def GetActiveProject(self) -> FakeObject | None:
        return self.active_project

    def GetActiveStudyCase(self) -> FakeObject | None:
        return self.active_study_case

    def GetCalcRelevantObjects(self, query: str, include_out_of_service: bool) -> list[FakeObject]:
        self.inventory_calls.append((query, include_out_of_service))
        return list(self.inventories[query.removeprefix("*.")])

    def GetFromStudyCase(self, class_name: str) -> FakeCommand | None:
        return self.command if class_name == "ComLdf" else None

    def PostCommand(self, command: str) -> int:
        self.post_commands.append(command)
        return 0

    def Show(self) -> None:
        self.show_count += 1
        raise AssertionError("Show must not be called")


class FakePowerFactoryModule(ModuleType):
    def __init__(self, application: FakeApplication) -> None:
        super().__init__("powerfactory")
        self.__file__ = "C:/fake/powerfactory.pyd"
        self.application = application
        self.application_calls: list[tuple[str, ...]] = []

    def GetApplicationExt(self, *args: str) -> FakeApplication:
        self.application_calls.append(args)
        return self.application


def _default_inventories() -> dict[str, list[FakeObject]]:
    return {
        "ElmLod": [FakeObject("Load B", "ElmLod", "Grid/Load B.ElmLod")],
        "ElmTerm": [
            FakeObject(
                "Terminal A",
                "ElmTerm",
                "Grid/Terminal A.ElmTerm",
                attributes={"m:u": 1.01},
                units={"m:u": "p.u."},
            )
        ],
        "ElmLne": [
            FakeObject(
                "Line A",
                "ElmLne",
                "Grid/Line A.ElmLne",
                attributes={"c:loading": 42.5},
                units={"c:loading": "%"},
            )
        ],
    }


class PowerFactory2026AdapterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.pyd_path = Path(self.temporary_directory.name) / "powerfactory.pyd"
        self.pyd_path.touch()

    def config(self, **overrides: Any) -> PowerFactory2026ProbeConfig:
        values: dict[str, Any] = {
            "pyd_path": str(self.pyd_path),
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
            "project_selector": "Project A",
            "study_case": "Case A",
            "sample_limit": 2,
            "cardinality_ceiling": 5,
            "include_out_of_service": False,
        }
        values.update(overrides)
        return PowerFactory2026ProbeConfig.from_mapping(values)

    def adapter(
        self,
        application: FakeApplication | None = None,
        *,
        config: PowerFactory2026ProbeConfig | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> tuple[PowerFactory2026LifecycleAdapter, FakePowerFactoryModule]:
        module = FakePowerFactoryModule(application or FakeApplication())
        adapter = PowerFactory2026LifecycleAdapter(
            config or self.config(),
            module_loader=lambda _: module,
            environ=environ,
        )
        return adapter, module

    def test_full_stage_flow_is_bounded_sorted_and_never_calls_show(self) -> None:
        inventories = _default_inventories()
        inventories["ElmLod"] = [
            FakeObject("Load B", "ElmLod", "Grid/B.ElmLod"),
            FakeObject("Load A", "ElmLod", "Grid/A.ElmLod"),
            FakeObject("Load C", "ElmLod", "Grid/C.ElmLod"),
        ]
        application = FakeApplication(inventories=inventories)
        adapter, module = self.adapter(application)

        self.assertEqual(module.application_calls, [])
        evidence_by_stage = {
            stage: adapter.execute_stage(stage)
            for stage in LifecycleStage
            if stage is not LifecycleStage.CLEANUP
        }

        load_sample = evidence_by_stage[LifecycleStage.INVENTORY]["classes"]["ElmLod"]["sample"]
        self.assertEqual([item["name"] for item in load_sample], ["Load A", "Load B"])
        self.assertEqual(
            evidence_by_stage[LifecycleStage.RESULTS]["terminal_voltage"]["unit"],
            "p.u.",
        )
        self.assertEqual(evidence_by_stage[LifecycleStage.RESULTS]["line_loading"]["unit"], "%")
        self.assertFalse(evidence_by_stage[LifecycleStage.IDENTITY]["stability_claimed"])
        self.assertTrue(
            all(
                item["read_only"]
                for item in evidence_by_stage[LifecycleStage.IDENTITY]["observations"]
            )
        )
        self._assert_plain_evidence(evidence_by_stage)

        cleanup = adapter.execute_stage(LifecycleStage.CLEANUP)
        self.assertEqual(application.show_count, 0)
        self.assertEqual(application.post_commands, [])
        self.assertFalse(cleanup["product_owned_exit_issued"])

    def test_module_load_and_connection_are_deferred_to_their_exact_stages(self) -> None:
        application = FakeApplication()
        module = FakePowerFactoryModule(application)
        load_calls: list[str] = []
        adapter = PowerFactory2026LifecycleAdapter(
            self.config(),
            module_loader=lambda path: load_calls.append(path) or module,
        )

        self.assertEqual(load_calls, [])
        self.assertEqual(module.application_calls, [])
        adapter.execute_stage(LifecycleStage.ENVIRONMENT)
        self.assertEqual(load_calls, [])
        adapter.execute_stage(LifecycleStage.IMPORT_MODULE)
        self.assertEqual(load_calls, [str(self.pyd_path)])
        self.assertEqual(module.application_calls, [])
        adapter.execute_stage(LifecycleStage.CONNECT_APPLICATION)
        self.assertEqual(module.application_calls, [(None, None, None)])

    def test_project_activation_uses_exact_configured_application_selector(self) -> None:
        application = FakeApplication()
        adapter, _ = self.adapter(application)

        for stage in (
            LifecycleStage.ENVIRONMENT,
            LifecycleStage.IMPORT_MODULE,
            LifecycleStage.CONNECT_APPLICATION,
            LifecycleStage.ACTIVATE_PROJECT,
        ):
            adapter.execute_stage(stage)

        self.assertEqual(application.current_user.calls, [("*.IntPrj", 1)])
        self.assertEqual(application.activate_project_calls, ["Project A"])

    def test_connection_always_uses_three_arguments_and_formats_ini(self) -> None:
        ini_path = Path(self.temporary_directory.name) / "probe.ini"
        ini_path.touch()
        adapter, module = self.adapter(config=self.config(ini_path=str(ini_path)))

        self._run_through_connect(adapter)

        self.assertEqual(
            module.application_calls,
            [(None, None, f'/ini "{ini_path}"')],
        )

    def test_ini_path_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            self.config(ini_path="not-an-ini.txt")

        adapter, _ = self.adapter(config=self.config(ini_path="missing.ini"))
        evidence = LifecycleProbeRunner(adapter).run()
        self.assertEqual(evidence.runs[0].failure_stage, LifecycleStage.ENVIRONMENT)

    def test_exact_match_missing_and_ambiguity_fail_closed(self) -> None:
        cases = (
            ("project missing", (), ("Case A",), LifecycleStage.ACTIVATE_PROJECT),
            (
                "project ambiguous",
                ("Project A", "Project A"),
                ("Case A",),
                LifecycleStage.ACTIVATE_PROJECT,
            ),
            (
                "study case missing",
                ("Project A",),
                (),
                LifecycleStage.ACTIVATE_STUDY_CASE,
            ),
            (
                "study case ambiguous",
                ("Project A",),
                ("Case A", "Case A"),
                LifecycleStage.ACTIVATE_STUDY_CASE,
            ),
        )
        for label, projects, study_cases, expected_stage in cases:
            with self.subTest(label=label):
                adapter, _ = self.adapter(
                    FakeApplication(
                        project_names=projects,
                        study_case_names=study_cases,
                    )
                )
                evidence = LifecycleProbeRunner(adapter).run()
                self.assertEqual(evidence.runs[0].failure_stage, expected_stage)

    def test_inventory_sample_limit_and_cardinality_ceiling(self) -> None:
        inventories = _default_inventories()
        inventories["ElmLod"] = [
            FakeObject(f"Load {index}", "ElmLod", f"Grid/{index}.ElmLod") for index in range(6)
        ]
        adapter, _ = self.adapter(
            FakeApplication(inventories=inventories),
            config=self.config(sample_limit=2, cardinality_ceiling=5),
        )

        evidence = LifecycleProbeRunner(adapter).run()

        self.assertEqual(evidence.runs[0].failure_stage, LifecycleStage.INVENTORY)
        stage = next(
            item for item in evidence.runs[0].stages if item.stage is LifecycleStage.INVENTORY
        )
        self.assertIn("exceeds ceiling 5", stage.error_message or "")

    def test_nonzero_execute_status_fails_load_flow(self) -> None:
        adapter, _ = self.adapter(FakeApplication(load_flow_return_code=7))

        evidence = LifecycleProbeRunner(adapter).run()

        self.assertEqual(evidence.runs[0].failure_stage, LifecycleStage.LOAD_FLOW)
        stage = next(
            item for item in evidence.runs[0].stages if item.stage is LifecycleStage.LOAD_FLOW
        )
        self.assertIn("nonzero status 7", stage.error_message or "")

    def test_attached_cleanup_restores_prior_project_then_study_case(self) -> None:
        application = FakeApplication()
        prior_project = FakeObject(
            "Prior Project",
            "IntPrj",
            "Projects/Prior.IntPrj",
            on_activate=application._set_active_project,
        )
        prior_study_case = FakeObject(
            "Prior Case",
            "IntCase",
            "Study/Prior.IntCase",
            on_activate=application._set_active_study_case,
        )
        application.active_project = prior_project
        application.active_study_case = prior_study_case
        adapter, _ = self.adapter(application)

        evidence = LifecycleProbeRunner(adapter).run()

        self.assertEqual(evidence.runs[0].status, StageStatus.PASS)
        cleanup = evidence.runs[0].stages[-1].data
        self.assertEqual(
            cleanup["actions"],
            ["restore_prior_project", "restore_prior_study_case"],
        )
        self.assertIs(application.active_project, prior_project)
        self.assertIs(application.active_study_case, prior_study_case)
        self.assertEqual(prior_project.activation_count, 1)
        self.assertEqual(prior_study_case.activation_count, 1)

    def test_active_context_selection_observes_the_existing_context_without_activation(
        self,
    ) -> None:
        application = FakeApplication()
        application.active_project = application.projects[0]
        application.active_study_case = application.study_cases[0]
        adapter, _ = self.adapter(
            application,
            config=self.config(project_selector="@active", study_case="@active"),
        )

        adapter.execute_stage(LifecycleStage.ENVIRONMENT)
        adapter.execute_stage(LifecycleStage.IMPORT_MODULE)
        adapter.execute_stage(LifecycleStage.CONNECT_APPLICATION)
        project = adapter.execute_stage(LifecycleStage.ACTIVATE_PROJECT)
        study_case = adapter.execute_stage(LifecycleStage.ACTIVATE_STUDY_CASE)
        cleanup = adapter.execute_stage(LifecycleStage.CLEANUP)

        self.assertEqual("active_context", project["selection_mode"])
        self.assertEqual("active_context", study_case["selection_mode"])
        self.assertEqual([], application.activate_project_calls)
        self.assertEqual([], cleanup["actions"])

    def test_failed_project_activation_does_not_deactivate_or_reactivate_prior_context(
        self,
    ) -> None:
        application = FakeApplication(project_activation_return_code=9)
        prior_project = FakeObject("Prior", "IntPrj", "Projects/Prior.IntPrj")
        prior_study_case = FakeObject("Prior Case", "IntCase", "Study/Prior.IntCase")
        application.active_project = prior_project
        application.active_study_case = prior_study_case
        adapter, _ = self.adapter(application)

        evidence = LifecycleProbeRunner(adapter).run()

        self.assertEqual(evidence.runs[0].failure_stage, LifecycleStage.ACTIVATE_PROJECT)
        self.assertIs(application.active_project, prior_project)
        self.assertIs(application.active_study_case, prior_study_case)
        self.assertEqual(prior_project.activation_count, 0)
        self.assertEqual(prior_project.deactivation_count, 0)
        self.assertEqual(application.projects[0].deactivation_count, 0)

    def test_study_case_already_activated_with_project_is_not_activated_twice(self) -> None:
        application = FakeApplication(study_case_activation_return_code=1)
        adapter, _ = self.adapter(application)

        for stage in (
            LifecycleStage.ENVIRONMENT,
            LifecycleStage.IMPORT_MODULE,
            LifecycleStage.CONNECT_APPLICATION,
            LifecycleStage.ACTIVATE_PROJECT,
        ):
            adapter.execute_stage(stage)
        application.active_study_case = application.study_cases[0]

        evidence = adapter.execute_stage(LifecycleStage.ACTIVATE_STUDY_CASE)

        self.assertFalse(evidence["activation_performed"])
        self.assertEqual(application.study_cases[0].activation_count, 0)
        self.assertIs(application.active_study_case, application.study_cases[0])

    def test_nonzero_study_case_activation_is_failure_and_selected_case_is_not_deactivated(
        self,
    ) -> None:
        application = FakeApplication(study_case_activation_return_code=4)
        prior_project = FakeObject(
            "Prior",
            "IntPrj",
            "Projects/Prior.IntPrj",
            on_activate=application._set_active_project,
        )
        prior_study_case = FakeObject(
            "Prior Case",
            "IntCase",
            "Study/Prior.IntCase",
            on_activate=application._set_active_study_case,
        )
        application.active_project = prior_project
        application.active_study_case = prior_study_case
        adapter, _ = self.adapter(application)

        evidence = LifecycleProbeRunner(adapter).run()

        self.assertEqual(evidence.runs[0].failure_stage, LifecycleStage.ACTIVATE_STUDY_CASE)
        stage = next(
            item
            for item in evidence.runs[0].stages
            if item.stage is LifecycleStage.ACTIVATE_STUDY_CASE
        )
        self.assertIn("nonzero status 4", stage.error_message or "")
        self.assertEqual(application.study_cases[0].deactivation_count, 0)
        self.assertIs(application.active_project, prior_project)
        self.assertIs(application.active_study_case, prior_study_case)

    def test_nonfinite_terminal_and_line_results_fail_closed(self) -> None:
        for class_name, attribute, value in (
            ("ElmTerm", "m:u", math.nan),
            ("ElmLne", "c:loading", math.inf),
        ):
            with self.subTest(class_name=class_name):
                inventories = _default_inventories()
                inventories[class_name][0].attributes[attribute] = value
                adapter, _ = self.adapter(FakeApplication(inventories=inventories))

                evidence = LifecycleProbeRunner(adapter).run()

                self.assertEqual(evidence.runs[0].failure_stage, LifecycleStage.RESULTS)
                stage = next(
                    item for item in evidence.runs[0].stages if item.stage is LifecycleStage.RESULTS
                )
                self.assertIn("is not finite", stage.error_message or "")

    def test_attached_cleanup_never_exits_and_is_idempotent(self) -> None:
        application = FakeApplication()
        adapter, _ = self.adapter(application)
        self._run_through_connect(adapter)

        first = adapter.execute_stage(LifecycleStage.CLEANUP)
        second = adapter.execute_stage(LifecycleStage.CLEANUP)

        self.assertEqual(application.post_commands, [])
        self.assertFalse(first["product_owned_exit_issued"])
        self.assertTrue(second["idempotent"])

    def test_product_owned_cleanup_posts_exit_exactly_once(self) -> None:
        application = FakeApplication()
        adapter, _ = self.adapter(
            application,
            config=replace(self.config(), session_ownership=SessionOwnership.PRODUCT_OWNED),
        )
        self._run_through_connect(adapter)

        first = adapter.execute_stage(LifecycleStage.CLEANUP)
        second = adapter.execute_stage(LifecycleStage.CLEANUP)

        self.assertEqual(application.post_commands, ["exit"])
        self.assertTrue(first["product_owned_exit_issued"])
        self.assertTrue(second["idempotent"])

    def test_optional_password_is_read_from_named_environment_variable_only(self) -> None:
        config = self.config(
            user_profile_env_var="PF_USER",
            password_env_var="PF_PASSWORD",
        )
        adapter, module = self.adapter(
            config=config,
            environ={"PF_USER": "operator", "PF_PASSWORD": "not-evidence"},
        )
        self._run_through_connect(adapter)

        self.assertEqual(
            module.application_calls,
            [("operator", "not-evidence", None)],
        )
        with self.assertRaises(ValueError):
            PowerFactory2026ProbeConfig.from_mapping(
                {
                    "pyd_path": str(self.pyd_path),
                    "python_version": config.python_version,
                    "project_selector": "Project A",
                    "study_case": "Case A",
                    "password": "not-allowed",
                }
            )

    def test_connection_failure_does_not_persist_vendor_error_text(self) -> None:
        secret = "must-not-enter-evidence"
        config = self.config(password_env_var="PF_PASSWORD")
        module = FakePowerFactoryModule(FakeApplication())

        def fail_connection(*_args: str) -> FakeApplication:
            raise RuntimeError(f"vendor included {secret}")

        module.GetApplicationExt = fail_connection  # type: ignore[method-assign]
        adapter = PowerFactory2026LifecycleAdapter(
            config,
            module_loader=lambda _: module,
            environ={"PF_PASSWORD": secret},
        )

        evidence = LifecycleProbeRunner(adapter).run()
        failure = next(
            stage
            for stage in evidence.runs[0].stages
            if stage.stage is LifecycleStage.CONNECT_APPLICATION
        )

        self.assertEqual("GetApplicationExt failed (RuntimeError)", failure.error_message)
        self.assertNotIn(secret, str(evidence.to_dict()))

    @staticmethod
    def _run_through_connect(adapter: PowerFactory2026LifecycleAdapter) -> None:
        for stage in (
            LifecycleStage.ENVIRONMENT,
            LifecycleStage.IMPORT_MODULE,
            LifecycleStage.CONNECT_APPLICATION,
        ):
            adapter.execute_stage(stage)

    def _assert_plain_evidence(self, value: Any) -> None:
        if value is None or isinstance(value, (str, bool, int, float)):
            return
        if isinstance(value, Mapping):
            self.assertTrue(all(isinstance(key, str) for key in value))
            for item in value.values():
                self._assert_plain_evidence(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                self._assert_plain_evidence(item)
            return
        self.fail(f"vendor object leaked into evidence: {type(value).__name__}")


if __name__ == "__main__":
    unittest.main()
