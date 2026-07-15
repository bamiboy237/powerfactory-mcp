"""Bounded, read-only inspection of the active PowerFactory context."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from powerfactory_agent.probes import (
    LifecycleAdapter,
    LifecycleStage,
    PowerFactory2026LifecycleAdapter,
    PowerFactory2026ProbeConfig,
    sanitize_evidence,
)

from .configuration import McpInstallation, load_probe_config


INSPECTION_CONTRACT_VERSION = "active-project-inspection/v1"
_ACTIVE_CONTEXT = "@active"
_INSPECTION_STAGES = (
    LifecycleStage.ENVIRONMENT,
    LifecycleStage.IMPORT_MODULE,
    LifecycleStage.CONNECT_APPLICATION,
    LifecycleStage.ACTIVATE_PROJECT,
    LifecycleStage.ACTIVATE_STUDY_CASE,
    LifecycleStage.INVENTORY,
)
AdapterFactory = Callable[[PowerFactory2026ProbeConfig], LifecycleAdapter]


def run_active_project_inspection(
    installation: McpInstallation,
    *,
    adapter_factory: AdapterFactory = PowerFactory2026LifecycleAdapter,
) -> dict[str, Any]:
    """Inspect only the already-active project and study case, then clean up."""

    config = load_probe_config(installation)
    if config.project_selector != _ACTIVE_CONTEXT or config.study_case != _ACTIVE_CONTEXT:
        raise ValueError(
            "active-project inspection requires project and study case selectors to be @active"
        )

    adapter = adapter_factory(config)
    stages: dict[str, Any] = {}
    failure_stage: str | None = None
    failure: dict[str, str] | None = None

    for stage in _INSPECTION_STAGES:
        try:
            stages[stage.value] = sanitize_evidence(adapter.execute_stage(stage))
        except Exception as exc:
            failure_stage = stage.value
            failure = {
                "error_type": type(exc).__name__,
                "message": str(sanitize_evidence(str(exc))),
            }
            break

    try:
        cleanup = sanitize_evidence(adapter.execute_stage(LifecycleStage.CLEANUP))
    except Exception as exc:
        cleanup = {
            "error_type": type(exc).__name__,
            "message": str(sanitize_evidence(str(exc))),
        }
        if failure_stage is None:
            failure_stage = LifecycleStage.CLEANUP.value
            failure = cleanup

    evidence: dict[str, Any] = {
        "schema_version": INSPECTION_CONTRACT_VERSION,
        "status": "PASS" if failure_stage is None else "FAIL",
        "read_only": True,
        "calculation_executed": False,
        "failure_stage": failure_stage,
        "failure": failure,
        "active_project": stages.get(LifecycleStage.ACTIVATE_PROJECT.value),
        "active_study_case": stages.get(LifecycleStage.ACTIVATE_STUDY_CASE.value),
        "inventory": stages.get(LifecycleStage.INVENTORY.value),
        "cleanup": cleanup,
    }
    evidence = sanitize_evidence(evidence)
    evidence_path = _write_inspection_evidence(installation, evidence)
    return {**evidence, "evidence_file": evidence_path.name}


def _write_inspection_evidence(
    installation: McpInstallation,
    evidence: dict[str, Any],
) -> Path:
    directory = installation.log_file.parent / "evidence"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = datetime.now(timezone.utc).strftime("inspection-%Y%m%dT%H%M%SZ.json")
    target = directory / filename
    target.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return target
