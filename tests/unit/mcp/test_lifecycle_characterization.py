"""Lifecycle characterization tests for the MCP server controller surface.

These tests pin invariants about lazy runtime creation, context admission,
replay, fail-closed behavior, shutdown, and the status-tool tool catalog.

Phase A only characterizes behavior; it intentionally does not change
production code. Invariants the current unsynchronized ``create_server``
implementation already satisfies are asserted directly. Invariants that
require the concurrency-safe controller (Phase B) or ASGI shutdown hookup
(Phase C) are recorded with ``unittest.expectedFailure`` so the suite stays
green while documenting the gap; those decorators are removed once the
responsible phase lands the safe behavior.

Inventory pagination cursor rejection (runtime ``_query_all_objects``) is
covered in Phase H, where the bounded-progress behavior is introduced; testing
it here would hang against the current unbounded loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import threading
import unittest

from starlette.testclient import TestClient

from powerfactory_agent.mcp.configuration import create_installation
from powerfactory_agent.mcp.server import build_asgi_app, create_server


class _RecordingRuntime:
    """Fake engineering runtime recording admission and cleanup calls."""

    def __init__(self) -> None:
        self.activate_calls = 0
        self.close_calls = 0

    def activate_context(self, *, project_selector: str, study_case: str) -> dict[str, object]:
        self.activate_calls += 1
        return {"status": "OK"}

    def get_model_context(self) -> dict[str, object]:
        return {"status": "OK"}

    def list_components(self, **_: object) -> dict[str, object]:
        return {"status": "OK"}

    def get_asset_context(self, **_: object) -> dict[str, object]:
        return {"status": "OK"}

    def run_validated_load_flow(self, **_: object) -> dict[str, object]:
        return {"status": "OK"}

    def get_calculation_run(self, **_: object) -> dict[str, object]:
        return {"status": "OK"}

    def compare_results(self, **_: object) -> dict[str, object]:
        return {"status": "OK"}

    def refresh_model_graph(self) -> dict[str, object]:
        return {"status": "OK"}

    def get_model_graph_summary(self) -> dict[str, object]:
        return {"status": "OK"}

    def query_model_graph(self, **_: object) -> dict[str, object]:
        return {"status": "OK"}

    def close(self) -> None:
        self.close_calls += 1


def _discovery(*_: object) -> dict[str, object]:
    return {"status": "PASS", "projects": [], "study_cases": []}


def _call(server, name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
    return asyncio.run(server.call_tool(name, arguments or {}))[1]


class ContextAdmissionLifecycleTests(unittest.TestCase):
    def test_repeating_the_same_confirmed_context_is_replay_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installation = create_installation(Path(directory) / "agent")
            runtime = _RecordingRuntime()
            server = create_server(
                installation,
                runtime_factory=lambda _: runtime,
                context_discovery=_discovery,
            )

            first = _call(
                server,
                "open_project_context",
                {"project_selector": "Project A", "study_case": "Case A", "confirmed": True},
            )
            second = _call(
                server,
                "open_project_context",
                {"project_selector": "Project A", "study_case": "Case A", "confirmed": True},
            )

            self.assertEqual("OK", first["status"])
            self.assertEqual("OK", second["status"])
            self.assertTrue(second["reused"])
            self.assertEqual(1, runtime.activate_calls)

    def test_a_different_second_context_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installation = create_installation(Path(directory) / "agent")
            runtime = _RecordingRuntime()
            server = create_server(
                installation,
                runtime_factory=lambda _: runtime,
                context_discovery=_discovery,
            )

            _call(
                server,
                "open_project_context",
                {"project_selector": "Project A", "study_case": "Case A", "confirmed": True},
            )
            rejected = _call(
                server,
                "open_project_context",
                {"project_selector": "Project B", "study_case": "Case B", "confirmed": True},
            )

            self.assertEqual("ERROR", rejected["status"])
            self.assertEqual("CONTEXT_ALREADY_ACTIVE", rejected["error"]["code"])
            self.assertEqual(1, runtime.activate_calls)

    def test_failed_runtime_construction_is_not_cached_as_a_valid_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installation = create_installation(Path(directory) / "agent")
            calls = {"count": 0}

            def factory(_: object) -> _RecordingRuntime:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("construction failed")
                return _RecordingRuntime()

            server = create_server(
                installation,
                runtime_factory=factory,
                context_discovery=_discovery,
            )

            first = _call(
                server,
                "open_project_context",
                {"project_selector": "Project A", "study_case": "Case A", "confirmed": True},
            )
            second = _call(
                server,
                "open_project_context",
                {"project_selector": "Project A", "study_case": "Case A", "confirmed": True},
            )

            self.assertEqual("ERROR", first["status"])
            self.assertEqual("ENGINEERING_TOOL_FAILED", first["error"]["code"])
            self.assertEqual("OK", second["status"])
            self.assertEqual(2, calls["count"])

    @unittest.expectedFailure
    def test_concurrent_confirmed_context_admission_creates_at_most_one_runtime(self) -> None:
        # Phase B unblocks this: a controller must serialize lazy runtime
        # creation so two concurrent confirmed admissions share one runtime
        # and one engine session. The barrier forces both threads into the
        # factory simultaneously, which the unsynchronized implementation
        # deterministically allows (two creations instead of one).
        with tempfile.TemporaryDirectory() as directory:
            installation = create_installation(Path(directory) / "agent")
            created: list[_RecordingRuntime] = []
            lock = threading.Lock()
            barrier = threading.Barrier(2, timeout=1.0)

            def factory(_: object) -> _RecordingRuntime:
                runtime = _RecordingRuntime()
                with lock:
                    created.append(runtime)
                barrier.wait()
                return runtime

            server = create_server(
                installation,
                runtime_factory=factory,
                context_discovery=_discovery,
            )

            def admit() -> None:
                asyncio.run(
                    server.call_tool(
                        "open_project_context",
                        {"project_selector": "Project A", "study_case": "Case A", "confirmed": True},
                    )
                )

            threads = [threading.Thread(target=admit), threading.Thread(target=admit)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)

            self.assertEqual(1, len(created))

    def test_status_tool_registered_tools_matches_actual_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installation = create_installation(Path(directory) / "agent")
            server = create_server(installation, context_discovery=_discovery)

            actual = sorted(tool.name for tool in asyncio.run(server.list_tools()))
            status = _call(server, "get_session_status")
            reported = sorted(status["registered_tools"])

            self.assertEqual(actual, reported)
            self.assertNotIn("approve", actual)
            self.assertNotIn("set_attribute", actual)


class ApplicationShutdownLifecycleTests(unittest.TestCase):
    def test_application_shutdown_is_harmless_when_no_runtime_was_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installation = create_installation(Path(directory) / "agent")
            starts: list[int] = []

            def factory(_: object) -> _RecordingRuntime:
                starts.append(1)
                return _RecordingRuntime()

            application = build_asgi_app(installation, runtime_factory=factory)

            with TestClient(application):
                pass

            self.assertEqual([], starts)

    @unittest.expectedFailure
    def test_application_shutdown_closes_a_started_runtime_exactly_once(self) -> None:
        # Phase C unblocks this: ASGI shutdown must invoke controller cleanup,
        # which closes the runtime exactly once. Today the lifespan does not
        # call close, so close_calls stays at zero.
        with tempfile.TemporaryDirectory() as directory:
            installation = create_installation(Path(directory) / "agent")
            runtime = _RecordingRuntime()

            application = build_asgi_app(
                installation,
                runtime_factory=lambda _: runtime,
                context_discovery=_discovery,
            )
            headers = {
                "Authorization": f"Bearer {installation.token_file.read_text().strip()}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Host": "127.0.0.1:8787",
            }

            with TestClient(application, base_url="http://127.0.0.1:8787") as client:
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
                response = client.post("/mcp", headers=headers, json=initialize)
                self.assertEqual(200, response.status_code)
                headers["mcp-session-id"] = response.headers["mcp-session-id"]
                client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "open_project_context",
                            "arguments": {
                                "project_selector": "Project A",
                                "study_case": "Case A",
                                "confirmed": True,
                            },
                        },
                    },
                )

            self.assertEqual(1, runtime.close_calls)


if __name__ == "__main__":
    unittest.main()