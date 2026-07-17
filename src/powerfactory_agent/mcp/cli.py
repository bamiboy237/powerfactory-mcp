"""Installation and launch commands for the local PowerFactory MCP service."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import uvicorn

from .configuration import configure_probe, create_installation, load_installation
from .inspection import write_single_context_discovery, write_single_project_inspection
from .probe import (
    run_acquisition_probe,
    run_connectivity_probe,
    write_single_acquisition_probe,
    write_single_connectivity_probe,
)
from .server import build_asgi_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="powerfactory-agent")
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser(
        "init", help="create local MCP configuration and bearer credential"
    )
    init_parser.add_argument("--state-dir", type=Path, required=True)
    init_parser.add_argument("--port", type=int, default=8787)

    configure_parser = commands.add_parser(
        "configure-probe", help="store real PowerFactory probe settings"
    )
    configure_parser.add_argument("--config", type=Path, required=True)
    configure_parser.add_argument("--pyd-path", required=True)
    configure_parser.add_argument("--python-version", required=True)
    configure_parser.add_argument("--project")
    configure_parser.add_argument("--study-case")
    configure_parser.add_argument("--ini-path")
    configure_parser.add_argument(
        "--session-ownership", choices=("attached", "product_owned"), default="attached"
    )
    configure_parser.add_argument("--user-profile-env-var")
    configure_parser.add_argument("--password-env-var")

    serve_parser = commands.add_parser(
        "serve", help="start authenticated Streamable HTTP MCP service"
    )
    serve_parser.add_argument("--config", type=Path, required=True)
    serve_parser.add_argument("--port", type=int)

    probe_parser = commands.add_parser(
        "probe", help="run the configured real PowerFactory lifecycle probe"
    )
    probe_parser.add_argument("--config", type=Path, required=True)
    probe_parser.add_argument("--repeat", type=int, default=2)

    acquisition_parser = commands.add_parser(
        "probe-acquisition", help="verify PowerFactory acquisition without selecting a project"
    )
    acquisition_parser.add_argument("--config", type=Path, required=True)

    worker_parser = commands.add_parser("_probe-once", help=argparse.SUPPRESS)
    worker_parser.add_argument("--probe-config", type=Path, required=True)
    worker_parser.add_argument("--output", type=Path, required=True)

    inspection_parser = commands.add_parser("_inspect-once", help=argparse.SUPPRESS)
    inspection_parser.add_argument("--probe-config", type=Path, required=True)
    inspection_parser.add_argument("--output", type=Path, required=True)

    discovery_parser = commands.add_parser("_discover-context", help=argparse.SUPPRESS)
    discovery_parser.add_argument("--probe-config", type=Path, required=True)
    discovery_parser.add_argument("--output", type=Path, required=True)
    discovery_parser.add_argument("--project")

    acquisition_worker_parser = commands.add_parser("_probe-acquisition-once", help=argparse.SUPPRESS)
    acquisition_worker_parser.add_argument("--probe-config", type=Path, required=True)
    acquisition_worker_parser.add_argument("--output", type=Path, required=True)

    show_parser = commands.add_parser(
        "show-install", help="print endpoint and Codex registration command"
    )
    show_parser.add_argument("--config", type=Path, required=True)

    arguments = parser.parse_args()
    if arguments.command == "_probe-once":
        if not write_single_connectivity_probe(arguments.probe_config, arguments.output):
            raise SystemExit(1)
        return
    if arguments.command == "_inspect-once":
        if not write_single_project_inspection(arguments.probe_config, arguments.output):
            raise SystemExit(1)
        return
    if arguments.command == "_discover-context":
        if not write_single_context_discovery(
            arguments.probe_config, arguments.output, arguments.project
        ):
            raise SystemExit(1)
        return
    if arguments.command == "_probe-acquisition-once":
        if not write_single_acquisition_probe(arguments.probe_config, arguments.output):
            raise SystemExit(1)
        return
    if arguments.command == "init":
        installation = create_installation(arguments.state_dir, port=arguments.port)
        config_path = arguments.state_dir.expanduser().resolve() / "powerfactory-agent.json"
        print(
            json.dumps(
                {"config": str(config_path), "endpoint": installation.endpoint_url}, sort_keys=True
            )
        )
        return
    if arguments.command == "configure-probe":
        target = configure_probe(
            arguments.config,
            {
                "pyd_path": arguments.pyd_path,
                "python_version": arguments.python_version,
                **(
                    {"project_selector": arguments.project, "study_case": arguments.study_case}
                    if arguments.project is not None or arguments.study_case is not None
                    else {}
                ),
                "ini_path": arguments.ini_path,
                "session_ownership": arguments.session_ownership,
                "user_profile_env_var": arguments.user_profile_env_var,
                "password_env_var": arguments.password_env_var,
            },
        )
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
    if arguments.command == "probe":
        payload = run_connectivity_probe(installation, arguments.repeat)
        print(json.dumps(payload, sort_keys=True))
        if payload["probe_status"] != "PASS":
            raise SystemExit(1)
        return
    if arguments.command == "probe-acquisition":
        payload = run_acquisition_probe(installation)
        print(json.dumps(payload, sort_keys=True))
        if payload["probe_status"] != "PASS":
            raise SystemExit(1)
        return
    if arguments.command == "serve":
        if arguments.port is not None:
            installation = installation.model_copy(update={"port": arguments.port})
        uvicorn.run(build_asgi_app(installation), host=installation.host, port=installation.port)
        return
    raise AssertionError(f"unsupported command: {arguments.command}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"powerfactory-agent: {error}", file=sys.stderr)
        raise SystemExit(2) from None
