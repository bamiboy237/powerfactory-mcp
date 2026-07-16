from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from powerfactory_agent.mcp.configuration import (
    configure_probe,
    create_installation,
    load_installation,
)
from powerfactory_agent.mcp.probe import _collect_isolated_runs, _run_isolated_probe


def _worker_evidence(status: str = "pass") -> dict[str, object]:
    return {
        "schema_version": 1,
        "runs": [
            {
                "run": 1,
                "status": status,
                "failure_stage": None if status == "pass" else "connect_application",
                "stages": [],
            }
        ],
    }


def test_repeated_real_probes_use_one_disposable_worker_per_run() -> None:
    with tempfile.TemporaryDirectory() as directory:
        installation = create_installation(Path(directory) / "state")
        calls: list[int] = []

        def worker(_installation: object) -> dict[str, object]:
            calls.append(len(calls) + 1)
            return _worker_evidence("fail" if len(calls) == 1 else "pass")

        evidence = _collect_isolated_runs(installation, 3, worker=worker)

    assert calls == [1, 2, 3]
    assert [run["run"] for run in evidence["runs"]] == [1, 2, 3]
    assert [run["status"] for run in evidence["runs"]] == ["fail", "pass", "pass"]


def test_isolated_probe_passes_the_persisted_probe_config_to_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        state_dir = Path(directory) / "state"
        create_installation(state_dir)
        pyd_path = Path(directory) / "powerfactory.pyd"
        pyd_path.touch()
        config_path = state_dir / "powerfactory-agent.json"
        probe_path = configure_probe(
            config_path,
            {
                "pyd_path": str(pyd_path),
                "python_version": "3.14",
                "project_selector": "Project A",
                "study_case": "Case A",
                "session_ownership": "product_owned",
            },
        )
        installation = load_installation(config_path)
        commands: list[list[str]] = []

        def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            output = Path(command[command.index("--output") + 1])
            output.write_text(json.dumps(_worker_evidence()), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr("powerfactory_agent.mcp.probe.subprocess.run", run)
        _run_isolated_probe(installation)

    assert len(commands) == 1
    assert commands[0][commands[0].index("--probe-config") + 1] == str(probe_path)


def test_probe_worker_rejects_malformed_evidence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        installation = create_installation(Path(directory) / "state")

        with pytest.raises(RuntimeError, match="exactly one lifecycle run"):
            _collect_isolated_runs(
                installation,
                1,
                worker=lambda _: {"schema_version": 1, "runs": []},
            )
