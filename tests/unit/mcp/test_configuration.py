from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from powerfactory_agent.mcp.configuration import (
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
