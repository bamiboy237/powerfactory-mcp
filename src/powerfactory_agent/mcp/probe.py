"""Real lifecycle probing, including load flow, shared by CLI and MCP."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from powerfactory_agent.probes import (
    LifecycleStage,
    LifecycleProbeRunner,
    PowerFactory2026LifecycleAdapter,
    PowerFactory2026ProbeConfig,
    write_evidence_json,
)

from .configuration import McpInstallation
from .server import MCP_CONTRACT_VERSION, _write_evidence

_PROBE_TIMEOUT_SECONDS = 180
_ACQUISITION_STAGES = (
    LifecycleStage.ENVIRONMENT,
    LifecycleStage.IMPORT_MODULE,
    LifecycleStage.CONNECT_APPLICATION,
)


def run_acquisition_probe(installation: McpInstallation) -> dict[str, Any]:
    """Verify one disposable PowerFactory acquisition without model context selection."""

    if installation.probe_config_file is None:
        raise ValueError("PowerFactory installation settings are not configured")
    with tempfile.TemporaryDirectory(prefix="powerfactory-acquisition-") as directory:
        output = Path(directory) / "evidence.json"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "powerfactory_agent.mcp.cli",
                "_probe-acquisition-once",
                "--probe-config",
                str(installation.probe_config_file),
                "--output",
                str(output),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        if not output.is_file():
            raise RuntimeError(
                f"PowerFactory acquisition worker exited with code {completed.returncode}"
            )
        try:
            evidence = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("PowerFactory acquisition worker wrote invalid evidence") from error
    evidence_path = _write_evidence(installation, evidence)
    return {
        "contract_version": MCP_CONTRACT_VERSION,
        "probe_status": "PASS" if evidence.get("status") == "PASS" else "FAIL",
        "evidence_file": evidence_path.name,
        "evidence": evidence,
    }


def write_single_acquisition_probe(probe_config_path: Path, output: Path) -> bool:
    """Run acquisition and cleanup in a disposable Python process."""

    config = PowerFactory2026ProbeConfig.from_json_file(probe_config_path)
    adapter = PowerFactory2026LifecycleAdapter(config)
    stages: dict[str, Any] = {}
    failure: str | None = None
    try:
        for stage in _ACQUISITION_STAGES:
            stages[stage.value] = adapter.execute_stage(stage)
    except Exception as exc:
        failure = type(exc).__name__
    try:
        cleanup = adapter.execute_stage(LifecycleStage.CLEANUP)
    except Exception as exc:
        cleanup = {"error_type": type(exc).__name__}
        failure = failure or type(exc).__name__
    payload = {
        "schema_version": "powerfactory-acquisition-probe/v1",
        "status": "PASS" if failure is None else "FAIL",
        "failure_type": failure,
        "stages": stages,
        "cleanup": cleanup,
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    return failure is None


def run_connectivity_probe(installation: McpInstallation, repeat: int) -> dict[str, Any]:
    """Execute each real PowerFactory lifecycle probe in a fresh process."""

    if not 1 <= repeat <= 3:
        raise ValueError("repeat must be between 1 and 3")
    evidence = _collect_isolated_runs(installation, repeat)
    evidence_path = _write_evidence(installation, evidence)
    return {
        "contract_version": MCP_CONTRACT_VERSION,
        "probe_status": (
            "PASS" if all(run["status"] == "pass" for run in evidence["runs"]) else "FAIL"
        ),
        "repeat": repeat,
        "evidence_file": evidence_path.name,
        "evidence": evidence,
    }


def write_single_connectivity_probe(probe_config_path: Path, output: Path) -> bool:
    """Run one native lifecycle in this disposable worker process."""

    probe_config = PowerFactory2026ProbeConfig.from_json_file(probe_config_path)
    evidence = LifecycleProbeRunner(PowerFactory2026LifecycleAdapter(probe_config)).run(1)
    write_evidence_json(evidence, output)
    return evidence.passed


def _collect_isolated_runs(
    installation: McpInstallation,
    repeat: int,
    *,
    worker: Callable[[McpInstallation], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    execute = worker or _run_isolated_probe
    runs: list[dict[str, Any]] = []
    for run_number in range(1, repeat + 1):
        document = execute(installation)
        if document.get("schema_version") != 1:
            raise RuntimeError("probe worker returned an unsupported evidence schema")
        worker_runs = document.get("runs")
        if not isinstance(worker_runs, list) or len(worker_runs) != 1:
            raise RuntimeError("probe worker must return exactly one lifecycle run")
        run = worker_runs[0]
        if not isinstance(run, dict) or run.get("status") not in {"pass", "fail"}:
            raise RuntimeError("probe worker returned malformed lifecycle evidence")
        runs.append({**run, "run": run_number})
    return {"schema_version": 1, "runs": runs}


def _run_isolated_probe(installation: McpInstallation) -> dict[str, Any]:
    if installation.probe_config_file is None:
        raise RuntimeError("PowerFactory probe is not configured")
    with tempfile.TemporaryDirectory(prefix="powerfactory-probe-") as directory:
        output = Path(directory) / "evidence.json"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "powerfactory_agent.mcp.cli",
                "_probe-once",
                "--probe-config",
                str(installation.probe_config_file),
                "--output",
                str(output),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        if not output.is_file():
            diagnostic = (completed.stderr or completed.stdout).strip()
            if len(diagnostic) > 500:
                diagnostic = diagnostic[-500:]
            suffix = f": {diagnostic}" if diagnostic else ""
            raise RuntimeError(
                f"PowerFactory probe worker exited with code {completed.returncode}{suffix}"
            )
        try:
            document = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("PowerFactory probe worker wrote invalid evidence") from error
        if not isinstance(document, dict):
            raise RuntimeError("PowerFactory probe worker evidence must be an object")
        return document
