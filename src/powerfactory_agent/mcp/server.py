"""Thin authenticated MCP adapter over real local PowerFactory setup services."""

from __future__ import annotations

from datetime import datetime, timezone
import hmac
import json
import logging
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from powerfactory_agent.probes import LifecycleProbeRunner, PowerFactory2026LifecycleAdapter

from .configuration import McpInstallation, load_probe_config, read_bearer_token


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
            return _error_response("UNAUTHENTICATED", "valid bearer authentication is required", 401)
        return await call_next(request)


def create_server(installation: McpInstallation) -> FastMCP:
    """Create the minimal real MCP product surface without fake model behavior."""

    logger = _configure_logger(installation.log_file)
    server = FastMCP(
        "powerfactory-agent",
        instructions=(
            "Safe PowerFactory MCP service. Use setup/status and the read-only "
            "connectivity probe only. No model mutation tools are registered."
        ),
        host=installation.host,
        port=installation.port,
        streamable_http_path="/mcp",
        json_response=True,
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
            "registered_tools": ["get_session_status", "run_powerfactory_connectivity_probe"],
            "mutation_tools_registered": False,
        }
        logger.info("mcp.get_session_status")
        return payload

    @server.tool()
    def run_powerfactory_connectivity_probe(repeat: int = 2) -> dict[str, object]:
        """Run the real read-only PowerFactory lifecycle probe and return sanitized evidence."""

        if not 1 <= repeat <= 3:
            raise ValueError("repeat must be between 1 and 3")
        probe_config = load_probe_config(installation)
        evidence = LifecycleProbeRunner(PowerFactory2026LifecycleAdapter(probe_config)).run(repeat)
        evidence_path = _write_evidence(installation, evidence.to_dict())
        logger.info("mcp.run_powerfactory_connectivity_probe status=%s", "pass" if evidence.passed else "fail")
        return {
            "contract_version": MCP_CONTRACT_VERSION,
            "probe_status": "PASS" if evidence.passed else "FAIL",
            "repeat": repeat,
            "evidence_file": evidence_path.name,
            "evidence": evidence.to_dict(),
        }

    return server


def build_asgi_app(installation: McpInstallation) -> Any:
    """Build the authenticated ASGI endpoint used by uvicorn."""

    server = create_server(installation)
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
    target.write_text(json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    return target


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {
            "contract_version": MCP_CONTRACT_VERSION,
            "error": {"code": code, "message": message},
        },
        status_code=status_code,
    )
