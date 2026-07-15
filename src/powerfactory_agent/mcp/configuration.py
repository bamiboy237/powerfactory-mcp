"""Local-only MCP installation state and secret-free PowerFactory probe setup."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from powerfactory_agent.probes import PowerFactory2026ProbeConfig


INSTALLATION_SCHEMA_VERSION = "powerfactory-agent-mcp-install/v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


class McpInstallation(BaseModel):
    """Secret-free configuration for the local MCP service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[INSTALLATION_SCHEMA_VERSION] = INSTALLATION_SCHEMA_VERSION
    host: Literal[DEFAULT_HOST] = DEFAULT_HOST
    port: int = Field(default=DEFAULT_PORT, ge=1024, le=65535)
    token_file: Path
    probe_config_file: Path | None = None
    log_file: Path

    @field_validator("token_file", "log_file")
    @classmethod
    def require_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("installation paths must be absolute")
        return value

    @field_validator("probe_config_file")
    @classmethod
    def require_absolute_optional_path(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("probe_config_file must be absolute")
        return value

    @property
    def endpoint_url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"


def create_installation(state_directory: str | Path, *, port: int = DEFAULT_PORT) -> McpInstallation:
    """Create an MCP installation without placing the bearer credential in config."""

    state_dir = Path(state_directory).expanduser().resolve()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _restrict_permissions(state_dir)

    token_file = state_dir / "mcp-token"
    if token_file.exists():
        raise FileExistsError(f"MCP token already exists: {token_file}")
    token_file.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
    _restrict_permissions(token_file)

    installation = McpInstallation(
        port=port,
        token_file=token_file,
        log_file=state_dir / "powerfactory-agent.log",
    )
    _write_json(_installation_path(state_dir), installation.model_dump(mode="json"))
    _write_probe_template(state_dir / "powerfactory-probe.example.json")
    return installation


def load_installation(path: str | Path) -> McpInstallation:
    """Load an exact installation document and require a private bearer credential."""

    installation_path = Path(path).expanduser().resolve()
    try:
        parsed = json.loads(installation_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read installation configuration: {installation_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("installation configuration must be JSON") from exc
    installation = McpInstallation.model_validate(parsed)
    _require_private_token_file(installation.token_file)
    return installation


def configure_probe(
    installation_path: str | Path,
    values: dict[str, object],
) -> Path:
    """Validate and store secret-free real-installation probe settings."""

    installation = load_installation(installation_path)
    probe = PowerFactory2026ProbeConfig.from_mapping(values)
    target = Path(installation_path).expanduser().resolve().parent / "powerfactory-probe.json"
    _write_json(target, {
        "pyd_path": probe.pyd_path,
        "python_version": probe.python_version,
        "project_selector": probe.project_selector,
        "study_case": probe.study_case,
        "sample_limit": probe.sample_limit,
        "cardinality_ceiling": probe.cardinality_ceiling,
        "include_out_of_service": probe.include_out_of_service,
        "session_ownership": probe.session_ownership.value,
        "ini_path": probe.ini_path,
        "user_profile_env_var": probe.user_profile_env_var,
        "password_env_var": probe.password_env_var,
    })
    updated = installation.model_copy(update={"probe_config_file": target})
    _write_json(Path(installation_path).expanduser().resolve(), updated.model_dump(mode="json"))
    return target


def read_bearer_token(installation: McpInstallation) -> str:
    _require_private_token_file(installation.token_file)
    token = installation.token_file.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("MCP bearer token is invalid")
    return token


def load_probe_config(installation: McpInstallation) -> PowerFactory2026ProbeConfig:
    if installation.probe_config_file is None:
        raise ValueError("PowerFactory probe is not configured")
    return PowerFactory2026ProbeConfig.from_json_file(installation.probe_config_file)


def _installation_path(state_dir: Path) -> Path:
    return state_dir / "powerfactory-agent.json"


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    _restrict_permissions(path)


def _write_probe_template(path: Path) -> None:
    _write_json(path, {
        "pyd_path": r"C:\\Program Files\\DIgSILENT\\PowerFactory 2026\\Python\\3.12\\powerfactory.pyd",
        "python_version": "3.12",
        "project_selector": "REPLACE_WITH_EXACT_PROJECT",
        "study_case": "REPLACE_WITH_EXACT_STUDY_CASE",
        "sample_limit": 10,
        "cardinality_ceiling": 10000,
        "include_out_of_service": False,
        "session_ownership": "attached",
    })


def _restrict_permissions(path: Path) -> None:
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))


def _require_private_token_file(path: Path) -> None:
    if not path.is_file():
        raise ValueError("MCP token file is missing")
    if os.name != "nt" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("MCP token file must not grant group or other permissions")
