"""Unvalidated PowerFactory 2026 adapter for the Buildout 0 lifecycle probe.

All PowerFactory API behavior in this module is a candidate contract. It must be
validated against the supported Windows installation before it is relied upon.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import platform
import re
import struct
import sys
import sysconfig
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any

from .lifecycle import LifecycleStage

_UNVALIDATED = "UNVALIDATED - Windows PowerFactory 2026 validation required"
_ACTIVE_CONTEXT = "@active"
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PYTHON_VERSION = re.compile(r"^[0-9]+\.[0-9]+$")


class PowerFactory2026ProbeError(RuntimeError):
    """A bounded, stage-specific candidate API operation failed."""


class SessionOwnership(str, Enum):
    ATTACHED = "attached"
    PRODUCT_OWNED = "product_owned"


@dataclass(frozen=True)
class PowerFactory2026ProbeConfig:
    """Secret-free configuration for one PowerFactory 2026 probe adapter."""

    pyd_path: str
    python_version: str
    project_selector: str | None = None
    study_case: str | None = None
    sample_limit: int = 10
    cardinality_ceiling: int = 10_000
    include_out_of_service: bool = False
    session_ownership: SessionOwnership = SessionOwnership.ATTACHED
    ini_path: str | None = None
    user_profile_env_var: str | None = None
    password_env_var: str | None = None

    def __post_init__(self) -> None:
        if not self.pyd_path or Path(self.pyd_path).name.casefold() != "powerfactory.pyd":
            raise ValueError("pyd_path must name powerfactory.pyd")
        if not _PYTHON_VERSION.fullmatch(self.python_version):
            raise ValueError("python_version must use major.minor form")
        if (self.project_selector is None) != (self.study_case is None):
            raise ValueError("project_selector and study_case must be configured together")
        for field_name, value in (
            ("project_selector", self.project_selector),
            ("study_case", self.study_case),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"{field_name} must be non-empty when configured")
        if isinstance(self.sample_limit, bool) or self.sample_limit < 1:
            raise ValueError("sample_limit must be a positive integer")
        if isinstance(self.cardinality_ceiling, bool) or self.cardinality_ceiling < 1:
            raise ValueError("cardinality_ceiling must be a positive integer")
        if self.sample_limit > self.cardinality_ceiling:
            raise ValueError("sample_limit must not exceed cardinality_ceiling")
        if not isinstance(self.include_out_of_service, bool):
            raise TypeError("include_out_of_service must be a boolean")
        if not isinstance(self.session_ownership, SessionOwnership):
            raise TypeError("session_ownership must be a SessionOwnership")
        if self.ini_path is not None:
            if not self.ini_path.strip() or Path(self.ini_path).suffix.casefold() != ".ini":
                raise ValueError("ini_path must name an .ini file")
        for field_name, environment_name in (
            ("user_profile_env_var", self.user_profile_env_var),
            ("password_env_var", self.password_env_var),
        ):
            if environment_name is not None and not _ENVIRONMENT_NAME.fullmatch(environment_name):
                raise ValueError(f"{field_name} must name an environment variable")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PowerFactory2026ProbeConfig:
        allowed = {
            "pyd_path",
            "python_version",
            "project_selector",
            "study_case",
            "sample_limit",
            "cardinality_ceiling",
            "include_out_of_service",
            "session_ownership",
            "ini_path",
            "user_profile_env_var",
            "password_env_var",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown probe configuration fields: {', '.join(unknown)}")
        if "password" in value or "credential" in value:
            raise ValueError("credentials are not permitted in probe configuration")

        data = dict(value)
        try:
            data["session_ownership"] = SessionOwnership(
                data.get("session_ownership", SessionOwnership.ATTACHED.value)
            )
            return cls(**data)
        except TypeError as exc:
            raise ValueError(f"invalid probe configuration: {exc}") from None

    @classmethod
    def from_json_file(cls, path: str | Path) -> PowerFactory2026ProbeConfig:
        with Path(path).open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
            raise ValueError("probe configuration JSON must be an object")
        return cls.from_mapping(value)

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> PowerFactory2026ProbeConfig:
        source = os.environ if environ is None else environ
        path = source.get("POWERFACTORY_PROBE_CONFIG")
        if not path:
            raise ValueError("POWERFACTORY_PROBE_CONFIG must point to a JSON file")
        return cls.from_json_file(path)


ModuleLoader = Callable[[str], ModuleType]


class PowerFactory2026LifecycleAdapter:
    """Staged, read-only candidate adapter for the PowerFactory 2026 API."""

    def __init__(
        self,
        config: PowerFactory2026ProbeConfig,
        *,
        module_loader: ModuleLoader | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._module_loader = module_loader or _load_powerfactory_module
        self._environ = os.environ if environ is None else environ
        self._module: ModuleType | None = None
        self._application: Any = None
        self._active_project: Any = None
        self._active_study_case: Any = None
        self._prior_project: Any = None
        self._prior_study_case: Any = None
        self._project_activated_by_probe = False
        self._study_case_activated_by_probe = False
        self._load_flow_command: Any = None
        self._sampled_objects: dict[str, list[Any]] = {}
        self._completed: set[LifecycleStage] = set()
        self._owned_exit_issued = False

    def execute_stage(self, stage: LifecycleStage) -> Mapping[str, Any]:
        handlers: dict[LifecycleStage, Callable[[], Mapping[str, Any]]] = {
            LifecycleStage.ENVIRONMENT: self._environment,
            LifecycleStage.IMPORT_MODULE: self._import_module,
            LifecycleStage.CONNECT_APPLICATION: self._connect_application,
            LifecycleStage.ACTIVATE_PROJECT: self._activate_project,
            LifecycleStage.ACTIVATE_STUDY_CASE: self._activate_study_case,
            LifecycleStage.INVENTORY: self._inventory,
            LifecycleStage.LOAD_FLOW: self._load_flow,
            LifecycleStage.RESULTS: self._results,
            LifecycleStage.CAPABILITIES: self._capabilities,
            LifecycleStage.IDENTITY: self._identity,
            LifecycleStage.CLEANUP: self._cleanup,
        }
        if not isinstance(stage, LifecycleStage):
            raise TypeError("stage must be a LifecycleStage")
        if stage is LifecycleStage.CLEANUP:
            return handlers[stage]()
        evidence = handlers[stage]()
        self._completed.add(stage)
        return evidence

    def discover_context_candidates(self, project_selector: str | None) -> Mapping[str, Any]:
        """Return bounded project or study-case choices without persisting a context choice."""

        self.execute_stage(LifecycleStage.ENVIRONMENT)
        self.execute_stage(LifecycleStage.IMPORT_MODULE)
        self.execute_stage(LifecycleStage.CONNECT_APPLICATION)
        application = self._application_required()
        user = _required_call(application, "GetCurrentUser")
        projects = self._bounded_contents(user, "*.IntPrj")[:100]
        if project_selector is None:
            return {
                "projects": tuple(_object_summary(project) for project in projects),
                "study_cases": (),
            }
        if not project_selector.strip():
            raise ValueError("project_selector must be non-empty when supplied")
        selected = self._select_exact(projects, project_selector, "project")
        self._prior_project = _required_call(application, "GetActiveProject")
        _ensure_activation_succeeded(
            _required_call(application, "ActivateProject", _object_key(selected)), "project"
        )
        self._active_project = _required_call(application, "GetActiveProject")
        if _object_key(self._active_project) != _object_key(selected):
            raise PowerFactory2026ProbeError("active project does not match the selected project")
        self._project_activated_by_probe = True
        study_folder = _required_call(application, "GetProjectFolder", "study")
        study_cases = self._bounded_contents(study_folder, "*.IntCase")[:100]
        return {
            "projects": ( _object_summary(selected), ),
            "study_cases": tuple(_object_summary(case) for case in study_cases),
        }

    def _environment(self) -> Mapping[str, Any]:
        if self._application is not None or self._module is not None:
            raise PowerFactory2026ProbeError(
                "environment stage requires cleanup of the previous run"
            )
        self._completed.clear()
        self._owned_exit_issued = False
        expected = tuple(int(part) for part in self._config.python_version.split("."))
        actual = sys.version_info[:2]
        if actual != expected:
            raise PowerFactory2026ProbeError(
                f"configured Python {self._config.python_version} does not match "
                f"running Python {actual[0]}.{actual[1]}"
            )
        module_path = Path(self._config.pyd_path)
        if not module_path.is_file():
            raise PowerFactory2026ProbeError("configured powerfactory.pyd does not exist")
        if self._config.ini_path is not None and not Path(self._config.ini_path).is_file():
            raise PowerFactory2026ProbeError("configured ini_path does not exist")
        return {
            "architecture": platform.machine(),
            "configured_release": "2026",
            "extension_suffix": sysconfig.get_config_var("EXT_SUFFIX"),
            "implementation_cache_tag": sys.implementation.cache_tag,
            "include_out_of_service": self._config.include_out_of_service,
            "pointer_width_bits": struct.calcsize("P") * 8,
            "powerfactory_pyd_path": str(module_path),
            "python_implementation": platform.python_implementation(),
            "python_version": self._config.python_version,
            "session_ownership": self._config.session_ownership.value,
            "validation_status": _UNVALIDATED,
        }

    def _import_module(self) -> Mapping[str, Any]:
        self._require(LifecycleStage.ENVIRONMENT)
        self._module = self._module_loader(self._config.pyd_path)
        get_application = getattr(self._module, "GetApplicationExt", None)
        if not callable(get_application):
            self._module = None
            raise PowerFactory2026ProbeError(
                "powerfactory module has no callable GetApplicationExt"
            )
        return {
            "get_application_ext_callable": True,
            "module_file": _plain_optional_string(getattr(self._module, "__file__", None)),
            "module_name": _plain_optional_string(getattr(self._module, "__name__", None)),
            "validation_status": _UNVALIDATED,
        }

    def _connect_application(self) -> Mapping[str, Any]:
        self._require(LifecycleStage.IMPORT_MODULE)
        if self._module is None:
            raise PowerFactory2026ProbeError("powerfactory module is unavailable")
        get_application = self._module.GetApplicationExt
        profile = self._environment_value(self._config.user_profile_env_var)
        password = self._environment_value(self._config.password_env_var)
        ini_argument = (
            f'/ini "{self._config.ini_path}"' if self._config.ini_path is not None else None
        )
        try:
            application = get_application(profile, password, ini_argument)
        except Exception as error:
            raise PowerFactory2026ProbeError(
                f"GetApplicationExt failed ({type(error).__name__})"
            ) from None
        finally:
            password = None
        if application is None:
            raise PowerFactory2026ProbeError("GetApplicationExt returned no application")
        self._application = application
        return {
            "application_acquired": True,
            "authentication_source": (
                "environment" if profile is not None or password is not None else "default"
            ),
            "validation_status": _UNVALIDATED,
        }

    def _activate_project(self) -> Mapping[str, Any]:
        self._require(LifecycleStage.CONNECT_APPLICATION)
        if self._config.project_selector is None:
            raise PowerFactory2026ProbeError("project context has not been selected")
        application = self._application_required()
        self._prior_project = _required_call(application, "GetActiveProject")
        self._prior_study_case = _required_call(application, "GetActiveStudyCase")
        if self._config.project_selector == _ACTIVE_CONTEXT:
            active = self._prior_project
            if active is None:
                raise PowerFactory2026ProbeError("no active project is available")
            self._active_project = active
            return {
                "active_project": _object_summary(active),
                "selection_mode": "active_context",
                "validation_status": _UNVALIDATED,
            }
        user = _required_call(application, "GetCurrentUser")
        candidates = self._bounded_contents(user, "*.IntPrj")
        selected = self._select_exact(candidates, self._config.project_selector, "project")
        activate_project = getattr(application, "ActivateProject", None)
        if not callable(activate_project):
            raise PowerFactory2026ProbeError("application has no callable ActivateProject")
        try:
            return_code = activate_project(self._config.project_selector)
        except Exception:
            raise PowerFactory2026ProbeError("project activation failed") from None
        _ensure_activation_succeeded(return_code, "project")
        active = _required_call(application, "GetActiveProject")
        if active is None or not _same_locator(active, selected):
            raise PowerFactory2026ProbeError(
                "active project does not match the exact selected project"
            )
        self._active_project = active
        self._project_activated_by_probe = True
        return {
            "active_project": _object_summary(active),
            "candidate_count": len(candidates),
            "exact_match_verified": True,
            "validation_status": _UNVALIDATED,
        }

    def _activate_study_case(self) -> Mapping[str, Any]:
        self._require(LifecycleStage.ACTIVATE_PROJECT)
        if self._config.study_case is None:
            raise PowerFactory2026ProbeError("study case context has not been selected")
        application = self._application_required()
        if self._config.study_case == _ACTIVE_CONTEXT:
            active = self._prior_study_case
            if active is None:
                raise PowerFactory2026ProbeError("no active study case is available")
            self._active_study_case = active
            return {
                "active_study_case": _object_summary(active),
                "selection_mode": "active_context",
                "validation_status": _UNVALIDATED,
            }
        folder = _required_call(application, "GetProjectFolder", "study")
        candidates = self._bounded_contents(folder, "*.IntCase")
        selected = self._select_exact(candidates, self._config.study_case, "study case")
        active = _required_call(application, "GetActiveStudyCase")
        activation_performed = active is None or not _same_locator(active, selected)
        if activation_performed:
            activate = getattr(selected, "Activate", None)
            if not callable(activate):
                raise PowerFactory2026ProbeError("study case has no callable Activate")
            try:
                return_code = activate()
            except Exception:
                raise PowerFactory2026ProbeError("study-case activation failed") from None
            _ensure_activation_succeeded(return_code, "study case")
            active = _required_call(application, "GetActiveStudyCase")
        if active is None or not _same_locator(active, selected):
            raise PowerFactory2026ProbeError(
                "active study case does not match the exact selected study case"
            )
        self._active_study_case = active
        self._study_case_activated_by_probe = activation_performed
        return {
            "active_study_case": _object_summary(active),
            "activation_performed": activation_performed,
            "candidate_count": len(candidates),
            "exact_match_verified": True,
            "validation_status": _UNVALIDATED,
        }

    def _inventory(self) -> Mapping[str, Any]:
        self._require(LifecycleStage.ACTIVATE_STUDY_CASE)
        application = self._application_required()
        inventories: dict[str, Any] = {}
        self._sampled_objects.clear()
        for class_name in ("ElmLod", "ElmTerm", "ElmLne"):
            values = _required_call(
                application,
                "GetCalcRelevantObjects",
                f"*.{class_name}",
                self._config.include_out_of_service,
            )
            objects = self._bounded_sequence(values, f"{class_name} inventory")
            objects.sort(key=_object_sort_key)
            sample = objects[: self._config.sample_limit]
            self._sampled_objects[class_name] = sample
            inventories[class_name] = {
                "count": len(objects),
                "sample": [_object_summary(item) for item in sample],
                "sample_truncated": len(objects) > len(sample),
            }
        return {
            "cardinality_ceiling_per_query": self._config.cardinality_ceiling,
            "classes": inventories,
            "include_out_of_service": self._config.include_out_of_service,
            "sample_limit_per_class": self._config.sample_limit,
            "validation_status": _UNVALIDATED,
        }

    def _load_flow(self) -> Mapping[str, Any]:
        self._require(LifecycleStage.INVENTORY)
        application = self._application_required()
        command = _required_call(application, "GetFromStudyCase", "ComLdf")
        if command is None:
            raise PowerFactory2026ProbeError("study case has no ComLdf command")
        execute = getattr(command, "Execute", None)
        if not callable(execute):
            raise PowerFactory2026ProbeError("ComLdf has no callable Execute")
        self._load_flow_command = command
        try:
            return_code = execute()
        except Exception:
            raise PowerFactory2026ProbeError("ComLdf Execute failed") from None
        if isinstance(return_code, bool) or not isinstance(return_code, int):
            raise PowerFactory2026ProbeError("ComLdf Execute returned a non-integer status")
        if return_code != 0:
            raise PowerFactory2026ProbeError(
                f"ComLdf Execute returned nonzero status {return_code}"
            )
        return {
            "command": _object_summary(command),
            "execute_return_code": return_code,
            "validation_status": _UNVALIDATED,
        }

    def _results(self) -> Mapping[str, Any]:
        self._require(LifecycleStage.LOAD_FLOW)
        terminals = self._sampled_objects.get("ElmTerm", [])
        lines = self._sampled_objects.get("ElmLne", [])
        if not terminals:
            raise PowerFactory2026ProbeError(
                "no sampled ElmTerm is available for m:u result retrieval"
            )
        if not lines:
            raise PowerFactory2026ProbeError(
                "no sampled ElmLne is available for c:loading result retrieval"
            )
        return {
            "line_loading": _numeric_result(lines[0], "c:loading"),
            "terminal_voltage": _numeric_result(terminals[0], "m:u"),
            "validation_status": _UNVALIDATED,
        }

    def _capabilities(self) -> Mapping[str, Any]:
        self._require(LifecycleStage.RESULTS)
        application = self._application_required()
        observations: list[dict[str, Any]] = []
        targets: list[tuple[str, Any, Sequence[str]]] = [
            (
                "module",
                self._module,
                ("GetApplicationExt",),
            ),
            (
                "application",
                application,
                (
                    "ActivateProject",
                    "GetActiveProject",
                    "GetActiveNetworkVariations",
                    "GetActiveScenario",
                    "GetActiveStages",
                    "GetActiveStudyCase",
                    "GetAttributeUnit",
                    "GetCalcRelevantObjects",
                    "GetCurrentUser",
                    "GetFromStudyCase",
                    "GetProjectFolder",
                    "PostCommand",
                    "PrintError",
                    "PrintInfo",
                    "PrintPlain",
                    "PrintWarn",
                    "FlushOutputWindow",
                ),
            ),
            (
                "project",
                self._active_project,
                (
                    "Activate",
                    "Deactivate",
                    "GetAttribute",
                    "GetClassName",
                    "GetFullName",
                    "GetParent",
                ),
            ),
            (
                "study_case",
                self._active_study_case,
                ("Activate", "Deactivate", "GetAttribute", "GetFullName", "GetParent"),
            ),
            (
                "load_flow_command",
                self._load_flow_command,
                ("Execute", "GetAttribute", "GetFullName"),
            ),
        ]
        for class_name in ("ElmLod", "ElmTerm", "ElmLne"):
            sample = self._sampled_objects.get(class_name, [])
            if sample:
                targets.append(
                    (
                        class_name,
                        sample[0],
                        (
                            "GetAttribute",
                            "GetAttributeUnit",
                            "GetClassName",
                            "GetFullName",
                            "GetParent",
                        ),
                    )
                )
        for target_name, target, methods in targets:
            for method in methods:
                observations.append(
                    {
                        "callable": callable(getattr(target, method, None)),
                        "method": method,
                        "target": target_name,
                    }
                )
        return {
            "observations": observations,
            "semantics_validated": False,
            "validation_status": _UNVALIDATED,
        }

    def _identity(self) -> Mapping[str, Any]:
        self._require(LifecycleStage.CAPABILITIES)
        observations: list[dict[str, Any]] = []
        for class_name in ("ElmLod", "ElmTerm", "ElmLne"):
            for item in self._sampled_objects.get(class_name, []):
                summary = _object_summary(item)
                observations.append(
                    {
                        "candidate_locators": [
                            {
                                "source": "GetFullName",
                                "value": summary["full_name"],
                            },
                            {"source": "loc_name", "value": summary["name"]},
                        ],
                        "class_name": class_name,
                        "read_only": True,
                    }
                )
        return {
            "observations": observations,
            "stability_claimed": False,
            "validation_status": _UNVALIDATED,
        }

    def _cleanup(self) -> Mapping[str, Any]:
        actions: list[str] = []
        failures: list[str] = []
        application = self._application
        if application is not None:
            if self._config.session_ownership is SessionOwnership.ATTACHED:
                self._restore_or_deactivate_attached_context(actions, failures)
            else:
                self._deactivate_probe_context(actions, failures)
        if (
            application is not None
            and self._config.session_ownership is SessionOwnership.PRODUCT_OWNED
            and not self._owned_exit_issued
        ):
            post_command = getattr(application, "PostCommand", None)
            if not callable(post_command):
                failures.append("post_exit_command_unavailable")
            else:
                try:
                    post_command("exit")
                    self._owned_exit_issued = True
                    actions.append("post_exit_command")
                except Exception:
                    failures.append("post_exit_command")

        self._active_study_case = None
        self._active_project = None
        self._prior_study_case = None
        self._prior_project = None
        self._study_case_activated_by_probe = False
        self._project_activated_by_probe = False
        self._load_flow_command = None
        self._sampled_objects.clear()
        self._application = None
        self._module = None
        self._completed.clear()
        if failures:
            raise PowerFactory2026ProbeError(
                f"cleanup operations failed: {', '.join(failures)}; "
                f"completed actions: {', '.join(actions) if actions else 'none'}"
            )
        return {
            "actions": actions,
            "attached_process_exited": False,
            "failures": failures,
            "idempotent": not actions,
            "product_owned_exit_issued": "post_exit_command" in actions,
            "validation_status": _UNVALIDATED,
        }

    def _restore_or_deactivate_attached_context(
        self, actions: list[str], failures: list[str]
    ) -> None:
        if not self._project_activated_by_probe:
            return
        if self._prior_project is None:
            self._deactivate_probe_context(actions, failures)
            return

        if not _attempt_context_action(
            self._prior_project, "Activate", "restore_prior_project", actions, failures
        ):
            self._deactivate_probe_context(actions, failures)
            return
        if self._prior_study_case is not None:
            _attempt_context_action(
                self._prior_study_case,
                "Activate",
                "restore_prior_study_case",
                actions,
                failures,
            )

    def _deactivate_probe_context(self, actions: list[str], failures: list[str]) -> None:
        if self._study_case_activated_by_probe and self._active_study_case is not None:
            _attempt_context_action(
                self._active_study_case,
                "Deactivate",
                "deactivate_probe_study_case",
                actions,
                failures,
            )
        if self._project_activated_by_probe and self._active_project is not None:
            _attempt_context_action(
                self._active_project,
                "Deactivate",
                "deactivate_probe_project",
                actions,
                failures,
            )

    def _application_required(self) -> Any:
        if self._application is None:
            raise PowerFactory2026ProbeError("application is unavailable")
        return self._application

    def _require(self, stage: LifecycleStage) -> None:
        if stage not in self._completed:
            raise PowerFactory2026ProbeError(f"stage {stage.value} must complete before this stage")

    def _environment_value(self, name: str | None) -> str | None:
        if name is None:
            return None
        value = self._environ.get(name)
        if value is None:
            raise PowerFactory2026ProbeError(f"required environment variable {name} is not set")
        return value

    def _bounded_contents(self, folder: Any, query: str) -> list[Any]:
        values = _required_call(folder, "GetContents", query, 1)
        return self._bounded_sequence(values, f"GetContents({query})")

    def _bounded_sequence(self, values: Any, label: str) -> list[Any]:
        if values is None:
            return []
        if isinstance(values, (str, bytes, bytearray)):
            raise PowerFactory2026ProbeError(f"{label} returned a non-object sequence")
        try:
            objects = list(values)
        except TypeError:
            raise PowerFactory2026ProbeError(f"{label} returned a non-sequence") from None
        if len(objects) > self._config.cardinality_ceiling:
            raise PowerFactory2026ProbeError(
                f"{label} cardinality {len(objects)} exceeds ceiling "
                f"{self._config.cardinality_ceiling}"
            )
        return objects

    @staticmethod
    def _select_exact(values: Sequence[Any], selector: str, label: str) -> Any:
        matches = [item for item in values if _matches_selector(item, selector)]
        if not matches:
            raise PowerFactory2026ProbeError(f"exact {label} selector has no match")
        if len(matches) > 1:
            raise PowerFactory2026ProbeError(
                f"exact {label} selector is ambiguous ({len(matches)} matches)"
            )
        return matches[0]


def create_powerfactory2026_adapter() -> PowerFactory2026LifecycleAdapter:
    """Create the first-party adapter from POWERFACTORY_PROBE_CONFIG."""

    return PowerFactory2026LifecycleAdapter(PowerFactory2026ProbeConfig.from_environment())


def _load_powerfactory_module(path: str) -> ModuleType:
    """Dynamically load exactly the configured extension module path."""

    requested = Path(path).resolve()
    existing = sys.modules.get("powerfactory")
    if existing is not None:
        existing_file = getattr(existing, "__file__", None)
        if existing_file is None or Path(existing_file).resolve() != requested:
            raise PowerFactory2026ProbeError("a different powerfactory module is already loaded")
        return existing

    spec = importlib.util.spec_from_file_location("powerfactory", requested)
    if spec is None or spec.loader is None:
        raise PowerFactory2026ProbeError(
            "could not create an import specification for powerfactory.pyd"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules["powerfactory"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop("powerfactory", None)
        raise PowerFactory2026ProbeError("powerfactory.pyd import failed") from None
    return module


def _required_call(target: Any, method_name: str, *args: Any) -> Any:
    method = getattr(target, method_name, None)
    if not callable(method):
        raise PowerFactory2026ProbeError(f"vendor object has no callable {method_name}")
    try:
        return method(*args)
    except Exception:
        raise PowerFactory2026ProbeError(f"{method_name} failed") from None


def _ensure_activation_succeeded(return_code: Any, label: str) -> None:
    if isinstance(return_code, int) and return_code != 0:
        raise PowerFactory2026ProbeError(
            f"{label} activation returned nonzero status {return_code}"
        )


def _attempt_context_action(
    target: Any,
    method_name: str,
    action: str,
    actions: list[str],
    failures: list[str],
) -> bool:
    method = getattr(target, method_name, None)
    if not callable(method):
        failures.append(f"{action}_unavailable")
        return False
    try:
        return_code = method()
    except Exception:
        failures.append(action)
        return False
    if isinstance(return_code, int) and return_code != 0:
        failures.append(action)
        return False
    actions.append(action)
    return True


def _plain_optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _object_name(value: Any) -> str | None:
    name = getattr(value, "loc_name", None)
    return name if isinstance(name, str) else None


def _object_full_name(value: Any) -> str | None:
    method = getattr(value, "GetFullName", None)
    if not callable(method):
        return None
    try:
        result = method()
    except Exception:
        return None
    return result if isinstance(result, str) else None


def _object_key(value: Any) -> str:
    key = _required_call(value, "GetFullName")
    if not isinstance(key, str) or not key:
        raise PowerFactory2026ProbeError("native object has no exact canonical locator")
    return key


def _object_class_name(value: Any) -> str | None:
    method = getattr(value, "GetClassName", None)
    if not callable(method):
        return None
    try:
        result = method()
    except Exception:
        return None
    return result if isinstance(result, str) else None


def _object_summary(value: Any) -> dict[str, Any]:
    return {
        "class_name": _object_class_name(value),
        "full_name": _object_full_name(value),
        "name": _object_name(value),
    }


def _object_sort_key(value: Any) -> tuple[str, str, str]:
    return (
        _object_full_name(value) or "",
        _object_name(value) or "",
        _object_class_name(value) or "",
    )


def _matches_selector(value: Any, selector: str) -> bool:
    return selector in {_object_full_name(value), _object_name(value)}


def _same_locator(left: Any, right: Any) -> bool:
    if left is right:
        return True
    left_full_name = _object_full_name(left)
    right_full_name = _object_full_name(right)
    return (
        left_full_name is not None
        and right_full_name is not None
        and left_full_name == right_full_name
    )


def _numeric_result(value: Any, attribute: str) -> dict[str, Any]:
    get_attribute = getattr(value, "GetAttribute", None)
    if not callable(get_attribute):
        raise PowerFactory2026ProbeError(
            f"result object has no callable GetAttribute for {attribute}"
        )
    try:
        raw_value = get_attribute(attribute)
    except Exception:
        raise PowerFactory2026ProbeError(
            f"result attribute {attribute} could not be read"
        ) from None
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise PowerFactory2026ProbeError(f"result attribute {attribute} is not numeric")
    numeric_value = float(raw_value)
    if not math.isfinite(numeric_value):
        raise PowerFactory2026ProbeError(f"result attribute {attribute} is not finite")

    unit: str | None = None
    get_unit = getattr(value, "GetAttributeUnit", None)
    if callable(get_unit):
        try:
            candidate = get_unit(attribute)
        except Exception:
            candidate = None
        if isinstance(candidate, str) and candidate:
            unit = candidate
    return {
        "attribute": attribute,
        "object": _object_summary(value),
        "unit": unit,
        "unit_available": unit is not None,
        "value": numeric_value,
    }
