# PowerFactory MCP

PowerFactory MCP is a local, authenticated MCP service for DIgSILENT
PowerFactory 2026. The current release candidate gives Codex three real tools:

- `get_session_status`
- `inspect_active_project`
- `run_powerfactory_connectivity_probe`

Active-project inspection loads the installed `powerfactory.pyd` and returns
bounded counts and samples for loads, terminals, and lines without executing a
calculation. The connectivity probe additionally executes the active study
case's load flow and samples voltage/loading results while verifying the full
PowerFactory lifecycle. Neither tool creates a sample network, chooses a
project, changes model attributes, or falls back to a simulated engine.

## Install on Windows

PowerFactory must be open with a safe project and study case active. Then run:

```powershell
git clone https://github.com/bamiboy237/powerfactory-mcp.git
cd powerfactory-mcp
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

The installer finds PowerFactory 2026, selects the matching Python runtime,
creates the local MCP configuration and credential, runs the real probe twice,
starts the loopback service, and registers it with Codex. Start Codex from the
same PowerShell window when the installer finishes.

The installer requires `git`, `uv`, and the Codex CLI. It fails closed if it
cannot find a compatible PowerFactory API, active context, valid licence, or a
working Codex installation.

## Engineer Test

Follow [the friend-test handoff](docs/friend-test.md). Return the tested commit
SHA and sanitized evidence. Never send the MCP token, credentials, licence
material, customer models, or unsanitized results.

## Current Product Status

This is a Windows friend-test release candidate, not the completed engineering
automation product. It is ready to test installation, authentication, Codex MCP
registration, active-model inspection, and the real PowerFactory lifecycle.
The persistent graph/query core exists, but the PowerFactory 2026 topology
extractor and stable identity registry are not yet validated, so graph tools are
not exposed with fixture or partial data. Preview, approval, and mutation tools
also remain gated.

Project execution state is recorded in
[`IMPLEMENTATION_CHECKLIST.md`](IMPLEMENTATION_CHECKLIST.md). Architecture and
acceptance evidence remain in `PRODUCT_ROADMAP.md` and `specs/`; those files are
maintainer documentation, not installation steps.
