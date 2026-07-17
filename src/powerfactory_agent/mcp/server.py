"""Thin authenticated MCP adapter over real local PowerFactory setup services."""

from __future__ import annotations

import hmac
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .configuration import McpInstallation, read_bearer_token
from .engineering import (
    ADMITTED_COMPONENT_ASSET_KINDS,
    EngineeringToolRuntime,
    build_engineering_runtime,
    validate_component_list_request,
)

MCP_CONTRACT_VERSION = "mcp-operation-contracts/v0.1.0"
_ALLOWED_ORIGINS = frozenset({"http://127.0.0.1", "http://localhost"})


class LocalBearerMiddleware(BaseHTTPMiddleware):
    """Require the installation token and reject browser origins outside loopback."""

    def __init__(self, app: Any, *, bearer_token: str) -> None:
        super().__init__(app)
        self._bearer_token = bearer_token

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and origin not in _ALLOWED_ORIGINS:
            return _error_response("ORIGIN_REJECTED", "request origin is not allowed", 403)
        authorization = request.headers.get("authorization", "")
        scheme, _, supplied_token = authorization.partition(" ")
        if scheme != "Bearer" or not hmac.compare_digest(supplied_token, self._bearer_token):
            return _error_response(
                "UNAUTHENTICATED", "valid bearer authentication is required", 401
            )
        return await call_next(request)


def create_server(
    installation: McpInstallation,
    *,
    runtime_factory: Callable[
        [McpInstallation], EngineeringToolRuntime
    ] = build_engineering_runtime,
) -> FastMCP:
    """Create the minimal real MCP product surface without fake model behavior."""

    logger = _configure_logger(installation.log_file)
    server = FastMCP(
        "powerfactory-agent",
        instructions=(
            "Safe PowerFactory MCP engineering service. Read active context, inventory, "
            "calculations, and bounded persisted topology. No mutation tools are registered."
        ),
        host=installation.host,
        port=installation.port,
        streamable_http_path="/mcp",
        json_response=True,
    )
    runtime: EngineeringToolRuntime | None = None

    def engineering_runtime() -> EngineeringToolRuntime:
        nonlocal runtime
        if runtime is None:
            runtime = runtime_factory(installation)
        return runtime

    def run_engineering_tool(name: str, operation: Callable[[], dict[str, object]]) -> dict[str, object]:
        """Contain ordinary Python tool exceptions without claiming native crash isolation."""

        try:
            return operation()
        except ValueError as exc:
            logger.info("mcp.%s rejected_request exception_type=%s", name, type(exc).__name__)
            return _tool_error("INVALID_ARGUMENT", str(exc))
        except Exception as exc:
            diagnostic = getattr(exc, "diagnostic", None)
            logger.error("mcp.%s failed exception_type=%s", name, type(exc).__name__)
            if isinstance(diagnostic, dict):
                return _tool_error(
                    "RUNTIME_OPERATION_FAILED",
                    "PowerFactory operation requires investigation; diagnostic evidence was persisted.",
                    diagnostic=diagnostic,
                )
            return _tool_error(
                "ENGINEERING_TOOL_FAILED",
                "The MCP server handled this tool exception; native host crashes require process isolation.",
            )

    @server.tool()
    def get_session_status() -> dict[str, object]:
        """Return local service configuration status; never starts PowerFactory."""

        payload = {
            "contract_version": MCP_CONTRACT_VERSION,
            "service": "powerfactory-agent",
            "transport": "streamable-http",
            "endpoint": installation.endpoint_url,
            "powerfactory_probe_configured": installation.probe_config_file is not None,
            "mcp_process": {"pid": os.getpid(), "alive": True},
            "admitted_component_asset_kinds": list(ADMITTED_COMPONENT_ASSET_KINDS),
            "registered_tools": [
                "compare_results",
                "get_asset_context",
                "get_calculation_run",
                "get_model_context",
                "get_model_graph_summary",
                "get_session_status",
                "inspect_active_project",
                "list_components",
                "query_model_graph",
                "refresh_model_graph",
                "run_powerfactory_connectivity_probe",
                "run_validated_load_flow",
            ],
            "mutation_tools_registered": False,
        }
        logger.info("mcp.get_session_status")
        return payload

    @server.tool()
    def get_model_context() -> dict[str, object]:
        """Return the verified active PowerFactory context and persisted extraction binding."""

        return run_engineering_tool("get_model_context", lambda: engineering_runtime().get_model_context())

    @server.tool()
    def list_components(
        asset_kind: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, object]:
        """List a bounded page of identified components from the active model."""

        return run_engineering_tool(
            "list_components",
            lambda: _list_components(
                engineering_runtime,
                asset_kind=asset_kind,
                limit=limit,
                cursor=cursor,
            ),
        )

    @server.tool()
    def get_asset_context(product_identity: str) -> dict[str, object]:
        """Return verified locator, attributes, and topology evidence for one product UUID."""

        return run_engineering_tool(
            "get_asset_context",
            lambda: engineering_runtime().get_asset_context(product_identity=product_identity),
        )

    @server.tool()
    def run_validated_load_flow(idempotency_key: str) -> dict[str, object]:
        """Run and persist a bounded load flow for the verified active model context."""

        return run_engineering_tool(
            "run_validated_load_flow",
            lambda: engineering_runtime().run_validated_load_flow(idempotency_key=idempotency_key),
        )

    @server.tool()
    def get_calculation_run(run_id: str) -> dict[str, object]:
        """Return one immutable persisted calculation run and its result reference."""

        return run_engineering_tool(
            "get_calculation_run",
            lambda: engineering_runtime().get_calculation_run(run_id=run_id),
        )

    @server.tool()
    def compare_results(
        baseline_snapshot_id: str,
        candidate_snapshot_id: str,
    ) -> dict[str, object]:
        """Compare two immutable result snapshots from the same verified context and policy."""

        return run_engineering_tool(
            "compare_results",
            lambda: engineering_runtime().compare_results(
                baseline_snapshot_id=baseline_snapshot_id,
                candidate_snapshot_id=candidate_snapshot_id,
            ),
        )

    @server.tool()
    def refresh_model_graph() -> dict[str, object]:
        """Persist a bounded graph of supported classes and report known coverage gaps."""

        return run_engineering_tool("refresh_model_graph", lambda: engineering_runtime().refresh_model_graph())

    @server.tool()
    def get_model_graph_summary() -> dict[str, object]:
        """Return the latest persisted topology revision and extraction counts."""

        return run_engineering_tool(
            "get_model_graph_summary", lambda: engineering_runtime().get_model_graph_summary()
        )

    @server.tool()
    def query_model_graph(
        query_kind: str,
        model_context_id: str,
        extraction_revision: int,
        limit: int = 25,
        center_identity: str | None = None,
        source_identity: str | None = None,
        target_identity: str | None = None,
        hops: int = 1,
    ) -> dict[str, object]:
        """Run a bounded components, neighborhood, or impact query on persisted topology."""

        return run_engineering_tool(
            "query_model_graph",
            lambda: engineering_runtime().query_model_graph(
                query_kind=query_kind,
                model_context_id=model_context_id,
                extraction_revision=extraction_revision,
                limit=limit,
                center_identity=center_identity,
                source_identity=source_identity,
                target_identity=target_identity,
                hops=hops,
            ),
        )

    @server.tool()
    def inspect_active_project() -> dict[str, object]:
        """Inspect bounded component counts and samples in the configured context."""

        from .inspection import run_active_project_inspection

        payload = run_active_project_inspection(installation)
        logger.info("mcp.inspect_active_project status=%s", payload["status"].lower())
        return payload

    @server.tool()
    def run_powerfactory_connectivity_probe(repeat: int = 2) -> dict[str, object]:
        """Verify the real lifecycle, including load flow, and return sanitized evidence."""

        from .probe import run_connectivity_probe

        payload = run_connectivity_probe(installation, repeat)
        logger.info(
            "mcp.run_powerfactory_connectivity_probe status=%s", payload["probe_status"].lower()
        )
        return payload

    return server


def build_asgi_app(
    installation: McpInstallation,
    *,
    runtime_factory: Callable[[McpInstallation], EngineeringToolRuntime] = build_engineering_runtime,
) -> Any:
    """Build the authenticated ASGI endpoint used by uvicorn."""

    server = create_server(installation, runtime_factory=runtime_factory)
    application = server.streamable_http_app()
    application.add_middleware(LocalBearerMiddleware, bearer_token=read_bearer_token(installation))
    return application


def _configure_logger(path: Path) -> logging.Logger:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    logger = logging.getLogger("powerfactory_agent.mcp")
    if not logger.handlers:
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def _write_evidence(installation: McpInstallation, evidence: dict[str, object]) -> Path:
    directory = installation.log_file.parent / "evidence"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = datetime.now(timezone.utc).strftime("connectivity-%Y%m%dT%H%M%SZ.json")
    target = directory / filename
    target.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    return target


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {
            "contract_version": MCP_CONTRACT_VERSION,
            "error": {"code": code, "message": message},
        },
        status_code=status_code,
    )


def _list_components(
    runtime_factory: Callable[[], EngineeringToolRuntime],
    *,
    asset_kind: str,
    limit: int,
    cursor: str | None,
) -> dict[str, object]:
    validate_component_list_request(asset_kind=asset_kind, limit=limit)
    return runtime_factory().list_components(asset_kind=asset_kind, limit=limit, cursor=cursor)


def _tool_error(
    code: str,
    message: str,
    *,
    diagnostic: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "ERROR",
        "contract_version": MCP_CONTRACT_VERSION,
        "error": {"code": code, "message": message},
    }
    if diagnostic is not None:
        payload["diagnostic"] = diagnostic
    return payload
