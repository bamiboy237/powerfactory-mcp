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

from .configuration import (
    McpInstallation,
    append_context_history,
    contextual_installation,
    count_context_history,
    read_bearer_token,
)
from .engineering import (
    ADMITTED_COMPONENT_ASSET_KINDS,
    EngineeringToolRuntime,
    build_engineering_runtime,
    validate_component_list_request,
)
from .inspection import discover_context_candidates

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
    context_discovery: Callable[[McpInstallation, str | None], dict[str, Any]] | None = None,
) -> FastMCP:
    """Create the minimal real MCP product surface without fake model behavior."""

    logger = _configure_logger(installation.log_file)
    server = FastMCP(
        "powerfactory-agent",
        instructions=(
            "Safe PowerFactory MCP engineering service. Call open_project_context before "
            "project-dependent reads, calculations, or topology. No mutation tools are registered."
        ),
        host=installation.host,
        port=installation.port,
        streamable_http_path="/mcp",
        json_response=True,
    )
    runtime: EngineeringToolRuntime | None = None
    active_context: dict[str, str] | None = None
    context_history: list[dict[str, str]] = []
    historical_context_count = count_context_history(installation)
    discover = context_discovery or (
        lambda selected_installation, project: discover_context_candidates(
            selected_installation, project_selector=project
        )
    )

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

    def run_contextual_tool(
        name: str, operation: Callable[[], dict[str, object]]
    ) -> dict[str, object]:
        if active_context is None:
            return _tool_error(
                "CONTEXT_REQUIRED",
                "Call open_project_context with an exact confirmed project and study case first.",
            )
        return run_engineering_tool(name, operation)

    @server.tool()
    def get_session_status() -> dict[str, object]:
        """Return local service configuration status; never starts PowerFactory."""

        payload = {
            "contract_version": MCP_CONTRACT_VERSION,
            "service": "powerfactory-agent",
            "transport": "streamable-http",
            "endpoint": installation.endpoint_url,
            "powerfactory_probe_configured": installation.probe_config_file is not None,
            "context_state": "ACTIVE" if active_context is not None else "CONTEXT_REQUIRED",
            "active_context": active_context,
            "context_history_count": historical_context_count + len(context_history),
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
                "open_project_context",
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
    def open_project_context(
        project_selector: str | None = None,
        study_case: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, object]:
        """Discover bounded choices or explicitly admit one exact PowerFactory project context."""

        nonlocal active_context
        if project_selector is not None and not project_selector.strip():
            return _tool_error("INVALID_ARGUMENT", "project_selector must be non-empty when supplied")
        if study_case is not None and not study_case.strip():
            return _tool_error("INVALID_ARGUMENT", "study_case must be non-empty when supplied")
        if project_selector is None or study_case is None or not confirmed:
            try:
                candidates = discover(installation, project_selector)
            except Exception as exc:
                logger.error("mcp.open_project_context discovery_failed exception_type=%s", type(exc).__name__)
                return _tool_error(
                    "CONTEXT_DISCOVERY_FAILED",
                    "PowerFactory context discovery failed; inspect sanitized evidence before retrying.",
                )
            return {
                "status": "CONTEXT_REQUIRED" if project_selector is None or study_case is None else "CONFIRMATION_REQUIRED",
                "contract_version": MCP_CONTRACT_VERSION,
                "candidates": candidates,
                "selected_project": project_selector,
                "selected_study_case": study_case,
            }
        requested = {"project_selector": project_selector, "study_case": study_case}
        if active_context is not None:
            if active_context == requested:
                return {
                    "status": "OK",
                    "contract_version": MCP_CONTRACT_VERSION,
                    "context": active_context,
                    "reused": True,
                }
            return _tool_error(
                "CONTEXT_ALREADY_ACTIVE",
                "This MCP session already owns a different active PowerFactory context.",
            )
        result = run_engineering_tool(
            "open_project_context",
            lambda: engineering_runtime().activate_context(
                project_selector=project_selector, study_case=study_case
            ),
        )
        if result.get("status") != "ERROR":
            active_context = requested
            context_history.append(requested)
            append_context_history(installation, requested)
        return result

    @server.tool()
    def get_model_context() -> dict[str, object]:
        """Return the verified active PowerFactory context and persisted extraction binding."""

        return run_contextual_tool("get_model_context", lambda: engineering_runtime().get_model_context())

    @server.tool()
    def list_components(
        asset_kind: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, object]:
        """List a bounded page of identified components from the active model."""

        return run_contextual_tool(
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

        return run_contextual_tool(
            "get_asset_context",
            lambda: engineering_runtime().get_asset_context(product_identity=product_identity),
        )

    @server.tool()
    def run_validated_load_flow(idempotency_key: str) -> dict[str, object]:
        """Run and persist a bounded load flow for the verified active model context."""

        return run_contextual_tool(
            "run_validated_load_flow",
            lambda: engineering_runtime().run_validated_load_flow(idempotency_key=idempotency_key),
        )

    @server.tool()
    def get_calculation_run(run_id: str) -> dict[str, object]:
        """Return one immutable persisted calculation run and its result reference."""

        return run_contextual_tool(
            "get_calculation_run",
            lambda: engineering_runtime().get_calculation_run(run_id=run_id),
        )

    @server.tool()
    def compare_results(
        baseline_snapshot_id: str,
        candidate_snapshot_id: str,
    ) -> dict[str, object]:
        """Compare two immutable result snapshots from the same verified context and policy."""

        return run_contextual_tool(
            "compare_results",
            lambda: engineering_runtime().compare_results(
                baseline_snapshot_id=baseline_snapshot_id,
                candidate_snapshot_id=candidate_snapshot_id,
            ),
        )

    @server.tool()
    def refresh_model_graph() -> dict[str, object]:
        """Persist a bounded graph of supported classes and report known coverage gaps."""

        return run_contextual_tool("refresh_model_graph", lambda: engineering_runtime().refresh_model_graph())

    @server.tool()
    def get_model_graph_summary() -> dict[str, object]:
        """Return the latest persisted topology revision and extraction counts."""

        return run_contextual_tool(
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

        return run_contextual_tool(
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
        """Decline the legacy disposable inspection path while a live session owns PowerFactory."""

        return _tool_error(
            "ENGINE_OPERATION_UNAVAILABLE",
            "inspect_active_project uses a disposable PowerFactory process and is disabled while "
            "the persistent MCP runtime owns the engine. Use get_model_context after "
            "open_project_context instead.",
        )

    @server.tool()
    def run_powerfactory_connectivity_probe(repeat: int = 2) -> dict[str, object]:
        """Decline the legacy disposable lifecycle path while a live session owns PowerFactory."""

        del repeat
        return _tool_error(
            "ENGINE_OPERATION_UNAVAILABLE",
            "run_powerfactory_connectivity_probe uses a disposable PowerFactory process and is "
            "disabled while the persistent MCP runtime owns the engine. Installer acquisition "
            "validation remains disposable before MCP startup.",
        )

    return server


def build_asgi_app(
    installation: McpInstallation,
    *,
    runtime_factory: Callable[[McpInstallation], EngineeringToolRuntime] = build_engineering_runtime,
    context_discovery: Callable[[McpInstallation, str | None], dict[str, Any]] | None = None,
) -> Any:
    """Build the authenticated ASGI endpoint used by uvicorn."""

    server = create_server(
        installation, runtime_factory=runtime_factory, context_discovery=context_discovery
    )
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
