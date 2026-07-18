# PowerFactory MCP

PowerFactory MCP is a local, authenticated MCP service for DIgSILENT
PowerFactory 2026. The friend-test release gives Codex real tools for:

- service and active-project inspection;
- stable component identity and bounded inventory;
- real load-flow execution, persisted results, and result comparison;
- a persisted supported-class topology graph with bounded queries.

Configured-project inspection loads the installed `powerfactory.pyd`, activates
only the exact configured project and study case in an isolated product-owned
engine, and returns bounded counts and samples without executing a calculation.
The connectivity probe additionally executes that study case's load flow and
samples voltage/loading results while verifying the full lifecycle. Neither
tool creates a sample network, changes model attributes, or falls back to a
simulated engine.

## Install on Windows

Close PowerFactory, open PowerShell, and run this single command:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force; $bootstrap = Join-Path $env:TEMP "powerfactory-mcp-bootstrap.ps1"; Invoke-WebRequest "https://raw.githubusercontent.com/bamiboy237/powerfactory-mcp/main/scripts/bootstrap-windows.ps1" -OutFile $bootstrap; & $bootstrap
```

The bootstrap stages each install in a unique managed attempt and validates the
PowerFactory ABI plus authenticated loopback MCP health without acquiring the
engine. During cutover it drains only the previously owned service, performs a
disposable acquisition probe, and atomically promotes the new release; a failed
cutover restarts the previous release. Installation does not select or persist a project or study case;
after launching Codex, use `open_project_context` to discover choices and then
explicitly confirm the exact project/study case for that MCP process.

The installer requires `git`, `uv`, and the Codex CLI. It fails closed if it
cannot find a compatible PowerFactory API, valid licence, or a working Codex
installation. Failed attempts retain only a sanitized transaction report under
`%LOCALAPPDATA%\PowerFactoryMCP\failure-reports`.

## Engineer Test

Follow [the friend-test handoff](docs/friend-test.md). Return the tested commit
SHA and sanitized evidence. Never send the MCP token, credentials, licence
material, customer models, or unsanitized results.

## Current Product Status

This is a Windows friend-test product, not a formal PowerFactory compatibility
release. It contains no fake runtime fallback. The first tester can install it,
inspect the active model, list and inspect identified assets, run load flows,
retrieve and compare results, and query a persisted supported-class graph.
Switches and three-winding transformers are not yet mapped, so graph responses
explicitly report incomplete topology. Preview, approval, and mutation tools
remain gated.

Project execution state is recorded in
[`IMPLEMENTATION_CHECKLIST.md`](IMPLEMENTATION_CHECKLIST.md). Architecture and
acceptance evidence remain in `PRODUCT_ROADMAP.md` and `specs/`; those files are
maintainer documentation, not installation steps.
