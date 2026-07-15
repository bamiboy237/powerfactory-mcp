"""Real lifecycle probing, including load flow, shared by CLI and MCP."""

from __future__ import annotations

from typing import Any

from powerfactory_agent.probes import LifecycleProbeRunner, PowerFactory2026LifecycleAdapter

from .configuration import McpInstallation, load_probe_config
from .server import MCP_CONTRACT_VERSION, _write_evidence


def run_connectivity_probe(installation: McpInstallation, repeat: int) -> dict[str, Any]:
    """Execute the configured real PowerFactory lifecycle probe and persist evidence."""

    if not 1 <= repeat <= 3:
        raise ValueError("repeat must be between 1 and 3")
    probe_config = load_probe_config(installation)
    evidence = LifecycleProbeRunner(PowerFactory2026LifecycleAdapter(probe_config)).run(repeat)
    evidence_path = _write_evidence(installation, evidence.to_dict())
    return {
        "contract_version": MCP_CONTRACT_VERSION,
        "probe_status": "PASS" if evidence.passed else "FAIL",
        "repeat": repeat,
        "evidence_file": evidence_path.name,
        "evidence": evidence.to_dict(),
    }
