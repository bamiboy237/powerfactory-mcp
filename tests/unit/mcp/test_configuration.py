from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from powerfactory_agent.mcp.configuration import (
    append_context_history,
    configure_probe,
    contextual_installation,
    count_context_history,
    create_installation,
    load_installation,
    read_bearer_token,
)


class McpInstallationTests(unittest.TestCase):
    def test_create_installation_generates_private_token_and_secret_free_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "agent"
            installation = create_installation(state_dir)

            loaded = load_installation(state_dir / "powerfactory-agent.json")

            self.assertEqual("http://127.0.0.1:8787/mcp", loaded.endpoint_url)
            self.assertEqual(installation.token_file, loaded.token_file)
            self.assertGreaterEqual(len(read_bearer_token(loaded)), 32)
            self.assertFalse(loaded.probe_config_file)
            self.assertTrue((state_dir / "powerfactory-probe.example.json").is_file())
            self.assertNotIn(read_bearer_token(loaded), (state_dir / "powerfactory-agent.json").read_text())
            if os.name != "nt":
                self.assertEqual(0, installation.token_file.stat().st_mode & 0o077)

    def test_rejects_token_file_with_group_or_other_permissions(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX permission validation is not available on Windows")
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "agent"
            installation = create_installation(state_dir)
            installation.token_file.chmod(0o644)

            with self.assertRaisesRegex(ValueError, "must not grant"):
                load_installation(state_dir / "powerfactory-agent.json")

    def test_installation_probe_settings_do_not_require_or_persist_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "agent"
            create_installation(state_dir)
            config_path = state_dir / "powerfactory-agent.json"
            probe_path = configure_probe(
                config_path,
                {
                    "pyd_path": "/tmp/powerfactory.pyd",
                    "python_version": "3.14",
                    "session_ownership": "product_owned",
                },
            )
            source = probe_path.read_text(encoding="utf-8")
            self.assertNotIn("project_selector", source)
            self.assertNotIn("study_case", source)
            installation = load_installation(config_path)
            with contextual_installation(
                installation, project_selector="CASE_1", study_case="CCT"
            ) as selected:
                self.assertNotEqual(installation.probe_config_file, selected.probe_config_file)
                self.assertIn("CASE_1", selected.probe_config_file.read_text(encoding="utf-8"))
            self.assertNotIn("CASE_1", probe_path.read_text(encoding="utf-8"))

    def test_context_history_never_restores_an_active_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installation = create_installation(Path(directory) / "agent")
            append_context_history(
                installation, {"project_selector": "CASE_1", "study_case": "CCT"}
            )
            self.assertEqual(1, count_context_history(installation))
