"""Bounded, read-only inspection of the configured PowerFactory context."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
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
CONTEXT_CANDIDATES_CONTRACT_VERSION = "powerfactory-context-candidates/v1"
_INSPECTION_TIMEOUT_SECONDS = 180
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
    adapter_factory: AdapterFactory | None = None,
) -> dict[str, Any]:
    """Inspect the exact configured context without running a calculation."""

    config = load_probe_config(installation)
    evidence = (
        _inspect_configured_context(config, adapter_factory)
        if adapter_factory is not None
        else _run_isolated_inspection(installation)
    )
    evidence_path = _write_inspection_evidence(installation, evidence)
    return {**evidence, "evidence_file": evidence_path.name}


def write_single_project_inspection(probe_config_path: Path, output: Path) -> bool:
    """Run one inspection in this disposable PowerFactory worker process."""

    config = PowerFactory2026ProbeConfig.from_json_file(probe_config_path)
    evidence = _inspect_configured_context(config, PowerFactory2026LifecycleAdapter)
    output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return evidence["status"] == "PASS"


def discover_context_candidates(
    installation: McpInstallation,
    *,
    project_selector: str | None,
) -> dict[str, Any]:
    """Discover bounded choices in an isolated process without storing a default context."""

    if installation.probe_config_file is None:
        raise RuntimeError("PowerFactory installation settings are not configured")
    with tempfile.TemporaryDirectory(prefix="powerfactory-context-candidates-") as directory:
        output = Path(directory) / "candidates.json"
        command = [
            sys.executable,
            "-m",
            "powerfactory_agent.mcp.cli",
            "_discover-context",
            "--probe-config",
            str(installation.probe_config_file),
            "--output",
            str(output),
        ]
        if project_selector is not None:
            command.extend(("--project", project_selector))
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=_INSPECTION_TIMEOUT_SECONDS,
        )
        if not output.is_file():
            raise RuntimeError(
                f"PowerFactory context discovery worker exited with code {completed.returncode}"
            )
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("PowerFactory context discovery worker wrote invalid evidence") from error
        if not isinstance(payload, dict) or payload.get("status") not in {"PASS", "FAIL"}:
            raise RuntimeError("PowerFactory context discovery worker returned malformed evidence")
        return payload


def write_single_context_discovery(
    probe_config_path: Path,
    output: Path,
    project_selector: str | None,
) -> bool:
    """Run one bounded candidate discovery in the disposable native worker."""

    config = PowerFactory2026ProbeConfig.from_json_file(probe_config_path)
    adapter = PowerFactory2026LifecycleAdapter(config)
    try:
        candidates = adapter.discover_context_candidates(project_selector)
        payload: dict[str, Any] = {
            "contract_version": CONTEXT_CANDIDATES_CONTRACT_VERSION,
            "status": "PASS",
            "project_selector": project_selector,
            **candidates,
        }
    except Exception as exc:
        payload = {
            "contract_version": CONTEXT_CANDIDATES_CONTRACT_VERSION,
            "status": "FAIL",
            "error_type": type(exc).__name__,
        }
    finally:
        try:
            payload["cleanup"] = sanitize_evidence(adapter.execute_stage(LifecycleStage.CLEANUP))
        except Exception as exc:
            payload["status"] = "FAIL"
            payload["cleanup"] = {"error_type": type(exc).__name__}
    output.write_text(
        json.dumps(sanitize_evidence(payload), indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return payload["status"] == "PASS"


def _inspect_configured_context(
    config: PowerFactory2026ProbeConfig,
    adapter_factory: AdapterFactory,
) -> dict[str, Any]:
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

    return sanitize_evidence(
        {
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
    )


def _run_isolated_inspection(installation: McpInstallation) -> dict[str, Any]:
    if installation.probe_config_file is None:
        raise RuntimeError("PowerFactory probe is not configured")
    with tempfile.TemporaryDirectory(prefix="powerfactory-inspection-") as directory:
        output = Path(directory) / "evidence.json"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "powerfactory_agent.mcp.cli",
                "_inspect-once",
                "--probe-config",
                str(installation.probe_config_file),
                "--output",
                str(output),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=_INSPECTION_TIMEOUT_SECONDS,
        )
        if not output.is_file():
            diagnostic = (completed.stderr or completed.stdout).strip()
            if len(diagnostic) > 500:
                diagnostic = diagnostic[-500:]
            suffix = f": {diagnostic}" if diagnostic else ""
            raise RuntimeError(
                f"PowerFactory inspection worker exited with code {completed.returncode}{suffix}"
            )
        try:
            evidence = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("PowerFactory inspection worker wrote invalid evidence") from error
        if not isinstance(evidence, dict) or evidence.get("status") not in {"PASS", "FAIL"}:
            raise RuntimeError("PowerFactory inspection worker returned malformed evidence")
        return evidence


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
