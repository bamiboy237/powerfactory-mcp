# Friend Test: PowerFactory MCP

This handoff tests a real, authenticated local MCP service against PowerFactory
2026. The test is successful only when installation, the real lifecycle probe,
service startup, Codex registration, and all three MCP tool calls work at the same
Git commit.

## Requirements

- Windows with DIgSILENT PowerFactory 2026 and a valid licence
- a safe, non-confidential project and study case
- Git, `uv`, and an authenticated Codex CLI available in PowerShell

## Automated Install

Open PowerFactory and activate the intended project and study case. Then open
PowerShell:

```powershell
git clone https://github.com/bamiboy237/powerfactory-mcp.git
cd powerfactory-mcp
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

The installer detects `PowerFactory 2026\Python\*\powerfactory.pyd`, creates a
matching environment, configures a local MCP credential, observes the active
PowerFactory context, runs the lifecycle probe twice, starts the loopback
service, and registers it with Codex. It does not select another model.

Keep this PowerShell window open. Codex must be started from it so the MCP
credential is inherited without being written into Codex configuration.

## Run The Test

Record the tested revision:

```powershell
git rev-parse HEAD
codex
```

In the new Codex thread:

1. Call `get_session_status` and confirm all three tools are registered.
2. Call `inspect_active_project` and confirm it reports the intended active
   project/study case plus bounded `ElmLod`, `ElmTerm`, and `ElmLne` inventory.
3. Call `run_powerfactory_connectivity_probe` with `repeat: 2`. This executes
   the active study case's load flow as part of lifecycle verification.
4. Confirm all calls came from the `powerfactory-agent` MCP server.

## Return Evidence

Return:

- the exact `git rev-parse HEAD` value
- PowerFactory release/service pack and Python version
- whether installation required any manual deviation
- all three MCP responses
- any crash, hang, licence failure, stale process, or GUI interaction
- `%LOCALAPPDATA%\PowerFactoryAgent\powerfactory-agent.log`
- `%LOCALAPPDATA%\PowerFactoryAgent\evidence\connectivity-*.json`
- `%LOCALAPPDATA%\PowerFactoryAgent\evidence\inspection-*.json`

Before sending files, inspect and redact customer names, local user paths, and
confidential model details. Never send `mcp-token`, PowerFactory credentials,
licence material, or a customer model. Preserve lifecycle stage names, return
codes, versions, counts, and error categories needed to diagnose a failure.

## Failure Handoff

Do not change the script, Python version, selected model, or PowerFactory
settings just to make a failed run pass. Return the failure evidence and the
exact manual deviation attempted. A structured `FAIL` is useful product
feedback; it is not a simulated result.

## Current Surface

The registered tools are `get_session_status`, `inspect_active_project`, and
`run_powerfactory_connectivity_probe`. Inspection is read-only and stops before
calculation. The connectivity probe executes a load flow but does not change
model attributes. The graph persistence/query core is not registered because a
real PowerFactory topology extractor and stable identity registry are not yet
validated. No preview, approval, or mutation tool is exposed.
