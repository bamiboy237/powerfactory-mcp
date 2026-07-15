from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from starlette.testclient import TestClient

from powerfactory_agent.mcp.configuration import create_installation
from powerfactory_agent.mcp.server import build_asgi_app


class McpTransportTests(unittest.TestCase):
    def test_transport_rejects_missing_or_invalid_bearer_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installation = create_installation(Path(directory) / "agent")
            application = build_asgi_app(installation)
            with TestClient(application) as client:
                response = client.post("/mcp")
                self.assertEqual(401, response.status_code)
                self.assertEqual("UNAUTHENTICATED", response.json()["error"]["code"])

                response = client.post("/mcp", headers={"Authorization": "Bearer invalid"})
                self.assertEqual(401, response.status_code)

    def test_transport_rejects_untrusted_browser_origin_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installation = create_installation(Path(directory) / "agent")
            application = build_asgi_app(installation)
            with TestClient(application) as client:
                response = client.post(
                    "/mcp",
                    headers={
                        "Authorization": f"Bearer {installation.token_file.read_text().strip()}",
                        "Origin": "https://untrusted.example",
                    },
                )
                self.assertEqual(403, response.status_code)
                self.assertEqual("ORIGIN_REJECTED", response.json()["error"]["code"])
