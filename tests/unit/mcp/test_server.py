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

    def test_authenticated_listener_stays_available_after_an_ordinary_runtime_exception(self) -> None:
        class FailingRuntime:
            def get_model_context(self):
                raise RuntimeError("native exception text must not reach the client")

        with tempfile.TemporaryDirectory() as directory:
            installation = create_installation(Path(directory) / "agent")
            starts = 0

            def factory(_):
                nonlocal starts
                starts += 1
                return FailingRuntime()

            application = build_asgi_app(installation, runtime_factory=factory)
            headers = {
                "Authorization": f"Bearer {installation.token_file.read_text().strip()}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Host": "127.0.0.1:8787",
            }
            initialize = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
            with TestClient(application, base_url="http://127.0.0.1:8787") as client:
                initialized = client.post("/mcp", headers=headers, json=initialize)
                self.assertEqual(200, initialized.status_code)
                headers["mcp-session-id"] = initialized.headers["mcp-session-id"]

                failed = client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "get_model_context", "arguments": {}},
                    },
                )
                self.assertEqual(200, failed.status_code)
                self.assertEqual(
                    "ENGINEERING_TOOL_FAILED",
                    failed.json()["result"]["structuredContent"]["error"]["code"],
                )
                self.assertNotIn("native exception text", failed.text)

                status = client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "get_session_status", "arguments": {}},
                    },
                )
                self.assertEqual(200, status.status_code)
                self.assertEqual(
                    "powerfactory-agent",
                    status.json()["result"]["structuredContent"]["service"],
                )
                self.assertEqual(1, starts)
