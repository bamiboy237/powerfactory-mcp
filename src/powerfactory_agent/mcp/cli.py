"""Installation and launch commands for the local PowerFactory MCP service."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import uvicorn

from .configuration import configure_probe, create_installation, load_installation
from .server import build_asgi_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="powerfactory-agent")
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="create local MCP configuration and bearer credential")
    init_parser.add_argument("--state-dir", type=Path, required=True)
    init_parser.add_argument("--port", type=int, default=8787)

    configure_parser = commands.add_parser("configure-probe", help="store real PowerFactory probe settings")
    configure_parser.add_argument("--config", type=Path, required=True)
    configure_parser.add_argument("--pyd-path", required=True)
    configure_parser.add_argument("--python-version", required=True)
    configure_parser.add_argument("--project", required=True)
    configure_parser.add_argument("--study-case", required=True)
    configure_parser.add_argument("--ini-path")
    configure_parser.add_argument("--session-ownership", choices=("attached", "product_owned"), default="attached")
    configure_parser.add_argument("--user-profile-env-var")
    configure_parser.add_argument("--password-env-var")

    serve_parser = commands.add_parser("serve", help="start authenticated Streamable HTTP MCP service")
    serve_parser.add_argument("--config", type=Path, required=True)

    show_parser = commands.add_parser("show-install", help="print endpoint and Codex registration command")
    show_parser.add_argument("--config", type=Path, required=True)

    arguments = parser.parse_args()
    if arguments.command == "init":
        installation = create_installation(arguments.state_dir, port=arguments.port)
        config_path = arguments.state_dir.expanduser().resolve() / "powerfactory-agent.json"
        print(json.dumps({"config": str(config_path), "endpoint": installation.endpoint_url}, sort_keys=True))
        return
    if arguments.command == "configure-probe":
        target = configure_probe(arguments.config, {
            "pyd_path": arguments.pyd_path,
            "python_version": arguments.python_version,
            "project_selector": arguments.project,
            "study_case": arguments.study_case,
            "ini_path": arguments.ini_path,
            "session_ownership": arguments.session_ownership,
            "user_profile_env_var": arguments.user_profile_env_var,
            "password_env_var": arguments.password_env_var,
        })
        print(json.dumps({"probe_config": str(target)}, sort_keys=True))
        return

    installation = load_installation(arguments.config)
    if arguments.command == "show-install":
        token_env = "POWERFACTORY_AGENT_MCP_TOKEN"
        print(f"set {token_env}=<contents of {installation.token_file}>")
        print(
            "codex mcp add powerfactory-agent --url "
            f"{installation.endpoint_url} --bearer-token-env-var {token_env}"
        )
        return
    if arguments.command == "serve":
        uvicorn.run(build_asgi_app(installation), host=installation.host, port=installation.port)
        return
    raise AssertionError(f"unsupported command: {arguments.command}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"powerfactory-agent: {error}", file=sys.stderr)
        raise SystemExit(2) from None
