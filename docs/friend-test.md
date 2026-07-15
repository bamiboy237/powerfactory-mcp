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

## Automated Install

Open PowerShell in the repository checkout and run one command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

The installer detects `PowerFactory 2026\Python\*\powerfactory.pyd`, creates a
matching `uv` environment, configures the protected local MCP token, uses the
currently active project and study case without selecting another model, runs
the real lifecycle probe twice, starts the MCP service, and registers it with
Codex CLI. Start Codex from that same PowerShell window so it inherits the
bearer-token environment variable.

The installer will stop with structured probe evidence instead of picking a
project or study case when PowerFactory has no active context. It does not use
sample data or a fallback engine.

## Run The Test

Start a new Codex CLI thread from the installer PowerShell window, call
`get_session_status`, then call
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
