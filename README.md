# PowerFactory MCP

Safe MCP-compatible workflows for DIgSILENT PowerFactory.

This project exposes a local MCP service for PowerFactory so an agent can inspect a configured project, query a bounded topology graph, and run load-flow analysis without inventing or mutating the model.

## What it does

- Inspect the active PowerFactory project and study case
- List and inspect identified assets with stable identities
- Execute real load flows and persist, retrieve, and compare results
- Query a supported-class topology graph with bounded results
- Keep execution inside the configured project/context and fail closed when requirements are not met

## Key safety rules

- No simulated fallback engine
- No automatic project or study-case selection on install
- No sample network creation or model mutation during inspection or analysis
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

If any prerequisite is missing or the ABI/license check fails, the install fails closed. Failed attempts retain a sanitized transaction report in `%LOCALAPPDATA%\PowerFactoryMCP\failure-reports`.

## Quick start

After install:

1. Launch Codex
2. Run `open_project_context` to discover available projects and study cases
3. Confirm the exact project and study case for that MCP process
4. Use the MCP tools to inspect assets, run load flows, and query graph data

## Capabilities

- **Project inspection**: bounded, deterministic metadata and asset counts without load flow
- **Load flow execution**: real, persisted analysis with voltage and loading results
- **Result comparison**: retrieve and compare persisted load-flow results
- **Topology queries**: bounded queries against a persisted supported-class graph

## Supported components

- Buses, lines, transformers (2-winding), generators, loads, shunt elements
- Full support for component properties, connections, and constraints

Currently in development:

- Switches and three-winding transformers (graph responses explicitly flag incomplete topology)
- Preview, approval, and mutation tools

## Project documentation

- `IMPLEMENTATION_CHECKLIST.md` — feature status and release tracking
- `PRODUCT_ROADMAP.md` — architecture and development roadmap
- `specs/` — technical specifications and acceptance criteria
- `AGENTS.md` — agent workflow documentation

## Why this exists

This project enforces a strict pattern: real PowerFactory access, bounded inspection, explicit context selection, and evidence-based validation. It is designed for safe agent workflows where the model can inspect and analyze a live network without drifting into unsafe or simulated behavior.
