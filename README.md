# PowerFactory MCP

PowerFactory MCP is a local, authenticated MCP service for DIgSILENT
PowerFactory 2026. The friend-test release gives Codex real tools for:

- service and active-project inspection;
- stable component identity and bounded inventory;
- real load-flow execution, persisted results, and result comparison;
- a persisted supported-class topology graph with bounded queries.

Active-project inspection loads the installed `powerfactory.pyd` and returns
bounded counts and samples for loads, terminals, and lines without executing a
calculation. The connectivity probe additionally executes the active study
case's load flow and samples voltage/loading results while verifying the full
PowerFactory lifecycle. Neither tool creates a sample network, chooses a
project, changes model attributes, or falls back to a simulated engine.

## Install on Windows

PowerFactory must be open with a safe project and study case active. Open
PowerShell and run this single command:

```powershell
irm https://raw.githubusercontent.com/bamiboy237/powerfactory-mcp/main/scripts/bootstrap-windows.ps1 | iex
```

The bootstrap downloads or updates the product. The guided installer then finds
PowerFactory 2026, selects the matching Python runtime, creates the protected
local MCP state and credential, runs the real probe twice, starts the loopback
service, and registers it with Codex. It prints the exact protected launcher
command to use when installation finishes.

The installer requires `git`, `uv`, and the Codex CLI. It fails closed if it
cannot find a compatible PowerFactory API, active context, valid licence, or a
working Codex installation.

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
