# Friend Test: PowerFactory MCP

This handoff tests a real, authenticated local MCP service against PowerFactory
2026. The test is successful when automated installation, service startup,
Codex registration, live inventory, calculation, persistence, and graph calls
work at the same Git commit.

## Requirements

- Windows with DIgSILENT PowerFactory 2026 and a valid licence
- a safe, non-confidential project and study case
- Git, `uv`, and an authenticated Codex CLI available in PowerShell

## Automated Install

Close PowerFactory, then open PowerShell. The installer will ask for the exact
non-confidential project and study-case names:

```powershell
$bootstrap = Join-Path $env:TEMP "powerfactory-mcp-bootstrap.ps1"; irm https://raw.githubusercontent.com/bamiboy237/powerfactory-mcp/main/scripts/bootstrap-windows.ps1 -OutFile $bootstrap; & $bootstrap
```

The installer detects `PowerFactory 2026\Python\*\powerfactory.pyd`, creates a
matching environment, configures a local MCP credential, activates only the
exact project and study case entered by the engineer, runs each lifecycle probe
in an isolated product-owned engine process, starts the loopback service, and
registers it with Codex.

Keep this PowerShell window open. Codex must be started from it so the MCP
credential is inherited without being written into Codex configuration.

## Run The Test

The installer prints a protected `Start-PowerFactoryCodex.ps1` command. Run it,
then record the tested revision from the installed source:

```powershell
git -C "$env:LOCALAPPDATA\PowerFactoryMCP\source" rev-parse HEAD
```

In the new Codex thread:

1. Call `get_session_status` and save the registered tool list.
2. Call `inspect_active_project` and confirm it reports the intended active
   project/study case plus bounded `ElmLod`, `ElmTerm`, and `ElmLne` inventory.
3. First call `list_components` with `{"asset_kind":"terminal","limit":100}`.
   This is the persistent-runtime retry. If it succeeds, call `get_model_context`,
   then list `area`, `line`, `load`, and `transformer`; save one returned product
   identity. `generator` is not admitted in this friend-test release.
4. Call `get_asset_context` with that identity.
5. Call `refresh_model_graph`, `get_model_graph_summary`, and
   `query_model_graph` with `query_kind: components` using the returned context
   ID and extraction revision.
6. Call `run_validated_load_flow` with a new idempotency key, then call
   `get_calculation_run` with its run ID. Repeat the load flow with another key
   and call `compare_results` using the two result snapshot IDs.
7. Call `run_powerfactory_connectivity_probe` with `repeat: 2`. This executes
   the active study case's load flow as part of lifecycle verification.
8. Confirm all calls came from the `powerfactory-agent` MCP server.

## Return Evidence

Return:

- the exact `git rev-parse HEAD` value
- PowerFactory release/service pack and Python version
- whether installation required any manual deviation
- all MCP responses from the sequence above
- any crash, hang, licence failure, stale process, or GUI interaction
- `%LOCALAPPDATA%\PowerFactoryAgent\powerfactory-agent.log`
- `%LOCALAPPDATA%\PowerFactoryAgent\evidence\connectivity-*.json`
- `%LOCALAPPDATA%\PowerFactoryAgent\evidence\inspection-*.json`

- `runtime-failure-*.json` from the configured state directory's `evidence`
  folder, if the persistent-runtime retry fails

Before sending files, inspect and redact customer names, local user paths, and
confidential model details. Never send `mcp-token`, PowerFactory credentials,
licence material, or a customer model. Preserve lifecycle stage names, return
codes, versions, counts, and error categories needed to diagnose a failure.

## Failure Handoff

Do not change the script, Python version, selected model, or PowerFactory
settings just to make a failed run pass. If the endpoint becomes unavailable
after the terminal inventory call, return the last operation ID and the sanitized
runtime-failure evidence; do not retry by starting another PowerFactory process.
Return the exact manual deviation attempted. A structured `FAIL` is useful
product feedback; it is not a simulated result.

## Current Surface

The product exposes status, inspection, component identity/context, load-flow
result persistence/comparison, and supported-class graph tools. The native
runtime has no fake fallback. Graph responses report that switches (`ElmCoup`),
three-winding transformers (`ElmTr3`), and explicit out-of-service-state
coverage are not yet admitted, so path claims are disabled. No preview,
approval, or mutation tool is exposed.
