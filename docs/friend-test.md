# Friend Test: PowerFactory MCP Bootstrap

This package starts a real, authenticated local MCP service. It does not create
sample networks, fabricate PowerFactory output, or register engineering write
tools. The initial friend-test verifies the actual PowerFactory 2026 Python API
connection and produces sanitized lifecycle evidence for the next iteration.

## Requirements

- Windows with DIgSILENT PowerFactory 2026 and a valid licence.
- A safe, non-confidential project and study case.
- `uv` installed.
- The Python version selected by `uv` must exactly match the version supported
  by that installation's `powerfactory.pyd`.
- Codex CLI installed and authenticated.

## Install And Configure

Open PowerShell in the repository checkout. Replace the sample Python version,
PowerFactory path, project, and study case with the real local values.

```powershell
uv sync --python 3.12
$state = Join-Path $env:LOCALAPPDATA "PowerFactoryAgent"
uv run powerfactory-agent init --state-dir $state --port 8787

uv run powerfactory-agent configure-probe `
  --config "$state\powerfactory-agent.json" `
  --pyd-path "C:\Program Files\DIgSILENT\PowerFactory 2026\Python\3.12\powerfactory.pyd" `
  --python-version 3.12 `
  --project "Exact Project Name" `
  --study-case "Exact Study Case Name"
```

Do not put a PowerFactory password in the JSON configuration. When a profile or
password is required, configure only the relevant environment-variable names
with `--user-profile-env-var` and `--password-env-var`, then set their values in
the PowerShell session before starting the server.

## Start And Register With Codex

Keep this terminal open while testing:

```powershell
$env:POWERFACTORY_AGENT_MCP_TOKEN = (Get-Content -Raw "$state\mcp-token").Trim()
uv run powerfactory-agent serve --config "$state\powerfactory-agent.json"
```

In a second PowerShell terminal, use the same token environment variable and
register the loopback service with Codex:

```powershell
$env:POWERFACTORY_AGENT_MCP_TOKEN = (Get-Content -Raw "$state\mcp-token").Trim()
codex mcp add powerfactory-agent --url http://127.0.0.1:8787/mcp --bearer-token-env-var POWERFACTORY_AGENT_MCP_TOKEN
```

Start a new Codex thread, call `get_session_status`, then call
`run_powerfactory_connectivity_probe` with `repeat: 2`.

## Return Evidence

Send the repository commit SHA, the MCP response, and these local files:

- `%LOCALAPPDATA%\PowerFactoryAgent\powerfactory-agent.log`
- `%LOCALAPPDATA%\PowerFactoryAgent\evidence\connectivity-*.json`

Never send `mcp-token`, a PowerFactory password, licence material, or
confidential project content. The probe records its own failures by lifecycle
stage; a `FAIL` result is useful evidence and does not imply a simulated
fallback was used.

## Current Surface

The registered tools are `get_session_status` and
`run_powerfactory_connectivity_probe`. The second tool is read-only and invokes
the real `powerfactory.pyd` lifecycle adapter. No inspection, calculation,
preview, approval, or mutation tool is registered until its PowerFactory-backed
contract is validated.
