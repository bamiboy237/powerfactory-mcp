# PowerFactory MCP

Safe MCP-compatible workflows for DIgSILENT PowerFactory.

This project exposes a local MCP service for PowerFactory so an agent can inspect a configured project, query a bounded topology graph, and run load-flow workflows without inventing or mutating the model.

## What it does

- Inspect the active PowerFactory project and study case
- List and inspect identified assets with stable identities
- Execute a real load flow and compare persisted results
- Query a supported-class topology graph with bounded results
- Keep execution inside the configured project/context and fail closed when requirements are not met

## Key safety rules

- No simulated fallback engine
- No automatic project or study-case selection on install
- No sample network creation or model mutation during inspection/probing
- Only the configured project/study case is activated in an isolated product-owned engine

## Install on Windows

Close PowerFactory, open PowerShell, and run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force; $bootstrap = Join-Path $env:TEMP "powerfactory-mcp-bootstrap.ps1"; Invoke-WebRequest "https://raw.githubusercontent.com/bamiboy237/powerfactory-mcp/main/scripts/powerfactory-mcp-bootstrap.ps1" -OutFile $bootstrap; & $bootstrap
```

The installer requires:

- Git
- uv
- Codex CLI
- a compatible PowerFactory API and valid license

If any prerequisite is missing or the ABI/license check fails, the install fails closed.

## Quick start

After install:

1. Launch Codex
2. Run `open_project_context` to discover available projects and study cases
3. Confirm the exact project and study case for that MCP process
4. Use the MCP tools to inspect assets, run load flows, and query graph data

## Current status

This is a Windows friend-test product, not a formal PowerFactory compatibility release.

It currently supports:

- project and study-case inspection
- bounded asset inventory and identity checks
- real load-flow execution and result comparison
- persisted supported-class topology queries

Known limitations:

- switches and three-winding transformers are not yet fully mapped
- graph responses explicitly flag incomplete topology
- preview, approval, and mutation tools remain gated

## Project documentation

- `docs/friend-test.md` — friend-test handoff and evidence requirements
- `IMPLEMENTATION_CHECKLIST.md` — execution status and release checklist
- `PRODUCT_ROADMAP.md` — architecture and buildout intent
- `specs/` — executable behavior and acceptance criteria

## Why this exists

This project follows a strict pattern: real PowerFactory access, bounded inspection, explicit context selection, and evidence-based validation. It is designed for safe agent workflows where the model can inspect and analyze a live network without drifting into unsafe or simulated behavior.
