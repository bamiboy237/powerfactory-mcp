from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile

from powerfactory_agent.mcp.configuration import create_installation
from powerfactory_agent.mcp.server import create_server


class RecordingEngineeringRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _call(self, name: str, **values: object) -> dict[str, object]:
        self.calls.append((name, values))
        return {"operation": name, **values}

    def get_model_context(self):
        return self._call("get_model_context")

    def list_components(self, **values):
        return self._call("list_components", **values)

    def get_asset_context(self, **values):
        return self._call("get_asset_context", **values)

    def run_validated_load_flow(self, **values):
        return self._call("run_validated_load_flow", **values)

    def get_calculation_run(self, **values):
        return self._call("get_calculation_run", **values)

    def compare_results(self, **values):
        return self._call("compare_results", **values)

    def refresh_model_graph(self):
        return self._call("refresh_model_graph")

    def get_model_graph_summary(self):
        return self._call("get_model_graph_summary")

    def query_model_graph(self, **values):
        return self._call("query_model_graph", **values)

    def close(self) -> None:
        self.calls.append(("close", {}))


def test_engineering_tool_catalog_is_registered_without_eager_runtime_start() -> None:
    with tempfile.TemporaryDirectory() as directory:
        installation = create_installation(Path(directory) / "agent")
        starts: list[RecordingEngineeringRuntime] = []
        server = create_server(
            installation,
            runtime_factory=lambda _: starts.append(RecordingEngineeringRuntime()) or starts[-1],
        )

        names = {tool.name for tool in asyncio.run(server.list_tools())}

        assert {
            "get_model_context",
            "list_components",
            "get_asset_context",
            "run_validated_load_flow",
            "get_calculation_run",
            "compare_results",
            "refresh_model_graph",
            "get_model_graph_summary",
            "query_model_graph",
        } <= names
        assert starts == []


def test_engineering_tools_share_one_lazy_runtime_and_forward_bounded_arguments() -> None:
    with tempfile.TemporaryDirectory() as directory:
        installation = create_installation(Path(directory) / "agent")
        runtime = RecordingEngineeringRuntime()
        starts = 0

        def factory(_):
            nonlocal starts
            starts += 1
            return runtime

        server = create_server(installation, runtime_factory=factory)

        asyncio.run(
            server.call_tool(
                "list_components",
                {"asset_kind": "line", "limit": 20, "cursor": None},
            )
        )
        asyncio.run(
            server.call_tool(
                "query_model_graph",
                {
                    "query_kind": "components",
                    "model_context_id": "10000000-0000-4000-8000-000000000001",
                    "extraction_revision": 1,
                    "limit": 25,
                    "center_identity": None,
                    "source_identity": None,
                    "target_identity": None,
                    "hops": 1,
                },
            )
        )

        assert starts == 1
        assert runtime.calls == [
            ("list_components", {"asset_kind": "line", "limit": 20, "cursor": None}),
            (
                "query_model_graph",
                {
                    "query_kind": "components",
                    "model_context_id": "10000000-0000-4000-8000-000000000001",
                    "extraction_revision": 1,
                    "limit": 25,
                    "center_identity": None,
                    "source_identity": None,
                    "target_identity": None,
                    "hops": 1,
                },
            ),
        ]
