"""Thin authenticated MCP adapter over real local PowerFactory setup services."""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
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

# Authoritative ordered tool-name catalog. The status tool reports this list
# verbatim and Phase A's catalog compatibility test asserts it equals the
# actual FastMCP registration, so the two cannot drift.
_REGISTERED_TOOL_NAMES = (
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
)


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


class SessionController:
    """Single owner of lazy runtime creation, context admission, and shutdown state.

    All mutation of the runtime reference, the active context, and the context
    history happens under one re-entrant lock. Admission (runtime creation and
    ``activate_context``) is serialized; ordinary inventory, graph, and
    calculation operations only borrow the runtime reference and run without
    holding the lock. The controller is the only owner of runtime/session
    state; MCP tool handlers remain thin delegations.
    """

    def __init__(
        self,
        installation: McpInstallation,
        *,
        runtime_factory: Callable[[McpInstallation], EngineeringToolRuntime],
        context_discovery: Callable[[McpInstallation, str | None], dict[str, Any]],
        historical_context_count: int,
        logger: logging.Logger,
    ) -> None:
        self._installation = installation
        self._runtime_factory = runtime_factory
        self._discover = context_discovery
        self._historical_context_count = historical_context_count
        self._logger = logger
        self._runtime: EngineeringToolRuntime | None = None
        self._active_context: dict[str, str] | None = None
        self._context_history: list[dict[str, str]] = []
        self._lock = threading.RLock()
        self._shutdown_started = False

    def status_payload(self) -> dict[str, object]:
        with self._lock:
            active_context = self._active_context
            context_history_count = self._historical_context_count + len(self._context_history)

        return {
            "contract_version": MCP_CONTRACT_VERSION,
            "service": "powerfactory-agent",
            "transport": "streamable-http",
            "endpoint": self._installation.endpoint_url,
            "powerfactory_probe_configured": self._installation.probe_config_file is not None,
            "context_state": "ACTIVE" if active_context is not None else "CONTEXT_REQUIRED",
            "active_context": active_context,
            "context_history_count": context_history_count,
            "mcp_process": {"pid": os.getpid(), "alive": True},
            "admitted_component_asset_kinds": list(ADMITTED_COMPONENT_ASSET_KINDS),
            "registered_tools": list(_REGISTERED_TOOL_NAMES),
            "mutation_tools_registered": False,
        }

    def open_project_context(
        self,
        project_selector: str | None,
        study_case: str | None,
        confirmed: bool,
    ) -> dict[str, object]:
        """Discover bounded choices or explicitly admit one exact PowerFactory context."""

        if project_selector is not None and not project_selector.strip():
            return _tool_error("INVALID_ARGUMENT", "project_selector must be non-empty when supplied")
        if study_case is not None and not study_case.strip():
            return _tool_error("INVALID_ARGUMENT", "study_case must be non-empty when supplied")
        if project_selector is None or study_case is None or not confirmed:
            try:
                candidates = self._discover(self._installation, project_selector)
            except Exception as exc:
                self._logger.error(
                    "mcp.open_project_context discovery_failed exception_type=%s",
                    type(exc).__name__,
                )
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
        with self._lock:
            if self._shutdown_started:
                return _tool_error(
                    "ENGINE_OPERATION_UNAVAILABLE",
                    "This MCP session is shutting down and cannot admit a new PowerFactory context.",
                )
            if self._active_context is not None:
                if self._active_context == requested:
                    return {
                        "status": "OK",
                        "contract_version": MCP_CONTRACT_VERSION,
                        "context": self._active_context,
                        "reused": True,
                    }
                return _tool_error(
                    "CONTEXT_ALREADY_ACTIVE",
                    "This MCP session already owns a different active PowerFactory context.",
                )

            def activate() -> dict[str, object]:
                if self._runtime is None:
                    self._runtime = self._runtime_factory(self._installation)
                return self._runtime.activate_context(
                    project_selector=project_selector, study_case=study_case
                )

            result = _run_engineering_tool(self._logger, "open_project_context", activate)
            if result.get("status") != "ERROR":
                self._active_context = requested
                self._context_history.append(requested)
                append_context_history(self._installation, requested)
            return result

    def require_runtime(self) -> EngineeringToolRuntime:
        """Return the shared engineering runtime, lazily creating it exactly once.

        The admission path creates the runtime before a context ever becomes
        active, so a contextual tool with an active context observes the
        existing runtime. Lazy creation is preserved for behavioral parity.
        """

        with self._lock:
            if self._runtime is None:
                self._runtime = self._runtime_factory(self._installation)
            return self._runtime

    def run_contextual_tool(
        self,
        name: str,
        operation: Callable[[], dict[str, object]],
    ) -> dict[str, object]:
        with self._lock:
            active_context = self._active_context
        if active_context is None:
            return _tool_error(
                "CONTEXT_REQUIRED",
                "Call open_project_context with an exact confirmed project and study case first.",
            )
        return _run_engineering_tool(self._logger, name, operation)

    def shutdown(self) -> None:
        """Reject further admission and close a started runtime at most once."""

        with self._lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            runtime = self._runtime
        if runtime is not None:
            _run_engineering_tool(self._logger, "shutdown", runtime.close)


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
    discover = context_discovery or (
        lambda selected_installation, project: discover_context_candidates(
            selected_installation, project_selector=project
        )
    )
    controller = SessionController(
        installation,
        runtime_factory=runtime_factory,
        context_discovery=discover,
        historical_context_count=count_context_history(installation),
        logger=logger,
    )

    @asynccontextmanager
    async def lifespan(_fastmcp: Any):
        # Startup is side-effect free: building the app never starts a
        # PowerFactory runtime. On shutdown, reject further admission and
        # submit gateway cleanup through the serialized owner exactly once.
        try:
            yield
        finally:
            controller.shutdown()

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
        lifespan=lifespan,
    )

    _register_context_and_status_tools(server, controller, logger)
    _register_inventory_tools(server, controller)
    _register_calculation_tools(server, controller)
    _register_graph_tools(server, controller)
    return server


def _register_context_and_status_tools(
    server: FastMCP,
    controller: SessionController,
    logger: logging.Logger,
) -> None:
    """Register status, context admission, and declined legacy lifecycle tools."""

    @server.tool()
    def get_session_status() -> dict[str, object]:
        """Return local service configuration status; never starts PowerFactory."""

        logger.info("mcp.get_session_status")
        return controller.status_payload()

    @server.tool()
    def open_project_context(
        project_selector: str | None = None,
        study_case: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, object]:
        """Discover bounded choices or explicitly admit one exact PowerFactory project context."""

        return controller.open_project_context(project_selector, study_case, confirmed)

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


def _register_inventory_tools(server: FastMCP, controller: SessionController) -> None:
    """Register read-only model and asset inventory tools."""

    @server.tool()
    def get_model_context() -> dict[str, object]:
        """Return the verified active PowerFactory context and persisted extraction binding."""

        return controller.run_contextual_tool(
            "get_model_context", lambda: controller.require_runtime().get_model_context()
        )

    @server.tool()
    def list_components(
        asset_kind: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, object]:
        """List a bounded page of identified components from the active model."""

        return controller.run_contextual_tool(
            "list_components",
            lambda: _list_components(
                controller.require_runtime,
                asset_kind=asset_kind,
                limit=limit,
                cursor=cursor,
            ),
        )

    @server.tool()
    def get_asset_context(product_identity: str) -> dict[str, object]:
        """Return verified locator, attributes, and topology evidence for one product UUID."""

        return controller.run_contextual_tool(
            "get_asset_context",
            lambda: controller.require_runtime().get_asset_context(product_identity=product_identity),
        )


def _register_calculation_tools(server: FastMCP, controller: SessionController) -> None:
    """Register load-flow calculation and result comparison tools."""

    @server.tool()
    def run_validated_load_flow(idempotency_key: str) -> dict[str, object]:
        """Run and persist a bounded load flow for the verified active model context."""

        return controller.run_contextual_tool(
            "run_validated_load_flow",
            lambda: controller.require_runtime().run_validated_load_flow(idempotency_key=idempotency_key),
        )

    @server.tool()
    def get_calculation_run(run_id: str) -> dict[str, object]:
        """Return one immutable persisted calculation run and its result reference."""

        return controller.run_contextual_tool(
            "get_calculation_run",
            lambda: controller.require_runtime().get_calculation_run(run_id=run_id),
        )

    @server.tool()
    def compare_results(
        baseline_snapshot_id: str,
        candidate_snapshot_id: str,
    ) -> dict[str, object]:
        """Compare two immutable result snapshots from the same verified context and policy."""

        return controller.run_contextual_tool(
            "compare_results",
            lambda: controller.require_runtime().compare_results(
                baseline_snapshot_id=baseline_snapshot_id,
                candidate_snapshot_id=candidate_snapshot_id,
            ),
        )


def _register_graph_tools(server: FastMCP, controller: SessionController) -> None:
    """Register persisted model-graph refresh, summary, and query tools."""

    @server.tool()
    def refresh_model_graph() -> dict[str, object]:
        """Persist a bounded graph of supported classes and report known coverage gaps."""

        return controller.run_contextual_tool(
            "refresh_model_graph", lambda: controller.require_runtime().refresh_model_graph()
        )

    @server.tool()
    def get_model_graph_summary() -> dict[str, object]:
        """Return the latest persisted topology revision and extraction counts."""

        return controller.run_contextual_tool(
            "get_model_graph_summary", lambda: controller.require_runtime().get_model_graph_summary()
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

        return controller.run_contextual_tool(
            "query_model_graph",
            lambda: controller.require_runtime().query_model_graph(
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


def _run_engineering_tool(
    logger: logging.Logger,
    name: str,
    operation: Callable[[], dict[str, object]],
) -> dict[str, object]:
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