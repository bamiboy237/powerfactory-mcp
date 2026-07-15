# PowerFactory Agent — Implementation Checklist

This file is only an execution index. Implementation details remain in [`PRODUCT_ROADMAP.md`](PRODUCT_ROADMAP.md) and the accepted companion specifications.

## Agent rules

- Track two independent frontiers:
  - the **acceptance frontier** is the first unchecked item whose prerequisite is checked;
  - the **development frontier** may prepare later platform-independent work when doing so does not invent PowerFactory evidence, accept a provisional specification, or enable dependent live behavior.
- Checklist boxes record accepted completion only. Prepared code and provisional specifications are recorded in the preparation table below, not by checking an item early.
- Open only the linked roadmap section; use `rg -n '^##|^###' PRODUCT_ROADMAP.md` to locate narrower subsections.
- Do not invent behavior covered by a required but unaccepted companion specification.
- A deliverable is complete only when its linked roadmap success criteria and dependency-matrix gate pass.
- Record commands and evidence in `specs/delivery/buildout-dependency-matrix.md` once created.
- Update Graphify after material code changes and record upstream reuse in the adoption ledger.

## Current frontiers

| Frontier | Current position | Meaning |
|---|---|---|
| Acceptance | Item 5 / Buildout 0 | `BLOCKED — Windows validation required`; the prepared probe is not accepted evidence. |
| Development | Buildout 2 owner integration, then Buildout 4 | The platform-independent owner/primitive boundary, provisional PowerFactory 2026 adapter, and Buildout 3 inventory are prepared. Next bind every adapter call to the durable owner, then build the persistent graph without enabling unvalidated live behavior. |

## Preparation record

| Scope | Preparation status | Acceptance status |
|---|---|---|
| Buildout 0 | Lifecycle, capability, cleanup, and identity probes are implemented with portable contract tests and a Windows handoff. | `BLOCKED — Windows validation required`; item 5 remains unchecked. |
| `DEP` and `ID` | Provisional `v0.1.0` specifications exist under `specs/architecture/` and separate deterministic requirements from Windows decision points. | Not accepted; Windows evidence must reconcile the open decision points before item 6 can be checked. |
| Buildout 1 / `DOM` | Immutable typed contracts, canonical JSON/digests, strict defaults, structured errors, and the deterministic bounded headline workflow are implemented. The additive provisional domain schema is `0.3.0` after admitting inventory and primitive-gateway DTOs. | Prepared, not accepted. B0, `DEP`, and `ID` acceptance prerequisites remain unsatisfied; item 7 stays unchecked. |
| Buildout 2 portable core | A durable SQLite operation store, bounded single-owner FIFO worker, separate queue/client/health deadlines, quarantine/reconciliation behavior, strict vendor-primitive protocol, deterministic primitive fake, and testable provisional `PowerFactoryGateway2026` adapter are implemented. The adapter uses an injected typed vendor seam, starts/closes idempotently, verifies context, bounds reads, separates commands/results, redacts logs, and disables writes. The older monolithic fake is explicitly retained only as `DeterministicHeadlineHarness` compatibility scaffolding. | Partially prepared, not accepted. Adapter calls are not yet composed through the durable owner; authenticated OS singleton/health path and all real lifecycle/concurrency evidence remain outstanding. Item 8 stays unchecked. |
| Buildout 3 portable core | Snapshot-bound model summary, bounded component pages, exact asset lookup, deterministic counts, and structured unsupported/unresolved warnings are implemented over a narrow read-only gateway view. | Prepared locally, not accepted. Native identity, class mapping, unsupported-object, pagination, and fixture accuracy evidence remain outstanding; item 9 stays unchecked. |

The headline harness's registered authorization, lock, counters, and direct-ledger behavior are test mechanisms only. They do not satisfy the future approval-authority, lease/fencing, durable workflow, persistence, or real mutation-strategy gates. The primitive fake's write path is disabled by default and exists only for guarded contract/failure tests.

Foundation milestone verification on 2026-07-14: `uv run pytest -q` (`106 passed`, `230 subtests`), generated schema `--check`, `compileall`, `git diff --check`, and `graphify update .` all passed. This is local preparation evidence, not acceptance evidence for any Windows-dependent gate.

Adapter milestone verification on 2026-07-14: `uv run pytest -q` (`113 passed`, `230 subtests`) and `graphify update .` passed. This is local preparation evidence, not acceptance evidence for any Windows-dependent gate.

## Delivery sequence

| Done | ID | Focused deliverable | Prerequisite check | Completion check | Roadmap source |
|---|---:|---|---|---|---|
| [x] | 1 | Initialize Git, `uv`, repository skeleton, Graphify, and agent integrations | None | Product repository and product graph are usable from a fresh agent session | [Repository structure and bootstrap](PRODUCT_ROADMAP.md#8-proposed-repository-structure) |
| [x] | 2 | Clone, pin, and Graphify PowerMCP and `powerfactory-tools` as sibling repositories | 1 checked | Separate source graphs work and inspected SHAs/licences are recorded | [Development bootstrap gate](PRODUCT_ROADMAP.md#development-bootstrap-gate) |
| [x] | 3 | Map upstream golden nuggets and rejected patterns in the adoption ledger | 2 checked | Every candidate has source, licence, classification, intended use, and validation plan | [Graph-Assisted Open-Source Adoption](PRODUCT_ROADMAP.md#12-graph-assisted-open-source-adoption) |
| [x] | 4 | Create the buildout dependency and evidence matrix | 3 checked | Every buildout has prerequisites, commands, evidence, and a decision gate | [Companion specification gate](PRODUCT_ROADMAP.md#companion-specification-gate) |
| [ ] | 5 | Prove the PowerFactory 2026 external-engine lifecycle | 4 checked; safe Windows fixture available | Repeated lifecycle probe passes and actual ABI/API/identity evidence is recorded | [Buildout 0](PRODUCT_ROADMAP.md#buildout-0--environment-and-api-capability-proof) |
| [ ] | 6 | Accept deployment/process-ownership and identity/revision specifications | 5 checked | Both specifications are accepted and grounded in probe evidence | [Runtime and State Model](PRODUCT_ROADMAP.md#4-runtime-and-state-model) |
| [ ] | 7 | Build typed domain contracts, generated schemas, and fake gateway | 6 checked | Buildout 1 tests and matrix gate pass without PowerFactory | [Buildout 1](PRODUCT_ROADMAP.md#buildout-1--typed-core-and-fake-gateway) |
| [ ] | 8 | Build the serialized PowerFactory 2026 gateway | 7 checked | Real gateway contract and lifecycle/concurrency gates pass | [Buildout 2](PRODUCT_ROADMAP.md#buildout-2--powerfactory-engine-gateway) |
| [ ] | 9 | Build the bounded read-only inventory and model summary | 8 checked | Identity, unsupported-element, pagination, and summary criteria pass | [Buildout 3](PRODUCT_ROADMAP.md#buildout-3--read-only-model-inventory) |
| [ ] | 10 | Build the persistent power-system graph | 9 checked | Restart, mismatch, bounded-query, and projection-rebuild gates pass | [Buildout 4](PRODUCT_ROADMAP.md#buildout-4--persistent-power-system-knowledge-graph) |
| [ ] | 11 | Accept `load-flow-and-violation-policy/v1` | 10 checked | Engineering limits, materiality, equivalence, baseline, and provenance rules are accepted | [Buildout 5 policy gate](PRODUCT_ROADMAP.md#buildout-5--calculation-and-result-overlays) |
| [ ] | 12 | Build calculations, immutable results, comparisons, and violation overlays | 11 checked | Buildout 5 success criteria and real calculation gate pass | [Buildout 5](PRODUCT_ROADMAP.md#buildout-5--calculation-and-result-overlays) |
| [ ] | 13 | Accept approval, context-lease, workflow-state-machine, and crash-reconciliation specifications | 12 checked | All four specifications are accepted with executable transitions/recovery rules | [Buildout 6 specification gate](PRODUCT_ROADMAP.md#buildout-6--durable-workflow-authorization-and-audit-foundation) |
| [ ] | 14 | Build durable workflow, authorization, leases, audit, and recovery | 13 checked | Buildout 6 restart, forgery, replay, reconciliation, and audit gates pass | [Buildout 6](PRODUCT_ROADMAP.md#buildout-6--durable-workflow-authorization-and-audit-foundation) |
| [ ] | 15 | Accept `area-load-scaling/v1` | 14 checked | Supported targets, arithmetic, overrides, membership, and exclusions are accepted | [Buildout 7 specification gate](PRODUCT_ROADMAP.md#buildout-7--area-load-scaling-preview) |
| [ ] | 16 | Build area-load-scaling preview | 15 checked | Preview is deterministic, explainable, bounded, and proven non-mutating | [Buildout 7](PRODUCT_ROADMAP.md#buildout-7--area-load-scaling-preview) |
| [ ] | 17 | Run the direct-ledger mutation experiment | 16 checked | Buildout 8 failure-injection, reconciliation, conflict, and restoration gates pass | [Buildout 8](PRODUCT_ROADMAP.md#buildout-8--direct-ledger-mutation-experiment) |
| [ ] | 18 | Run the scenario-isolation mutation experiment | 16 checked; same foundation as 17 | Buildout 9 isolation, reactivation, cleanup, and leakage gates pass | [Buildout 9](PRODUCT_ROADMAP.md#buildout-9--scenario-isolation-mutation-experiment) |
| [ ] | 19 | Select the mutation strategy per supported attribute category | 17 and 18 checked | Evidence-backed capability matrix and fallback decisions are accepted | [Selection policy](PRODUCT_ROADMAP.md#74-selection-policy) |
| [ ] | 20 | Compose the complete engineering application workflow | 19 checked | Real context-to-rollback workflow passes independently of MCP | [Buildout 10](PRODUCT_ROADMAP.md#buildout-10--complete-engineering-workflow) |
| [ ] | 21 | Accept versioned MCP request/response schemas | 20 checked | Tool, error, pagination, timeout, idempotency, and compatibility contracts are accepted | [Buildout 11 contracts](PRODUCT_ROADMAP.md#buildout-11--thin-mcp-surface-and-codex-integration) |
| [ ] | 22 | Expose the thin authenticated MCP surface to Codex | 21 checked | Codex and a second MCP client pass Buildout 11 gates with no approval tool | [Buildout 11](PRODUCT_ROADMAP.md#buildout-11--thin-mcp-surface-and-codex-integration) |
| [ ] | 23 | Build the activity graph and cross-agent briefing | 22 checked | Fresh-thread briefing and deterministic activity-graph rebuild pass | [Buildout 12](PRODUCT_ROADMAP.md#buildout-12--activity-graph-and-cross-agent-continuity) |
| [ ] | 24 | Build evidence-based load-flow diagnostics | 23 checked | Diagnostic provenance and read-only/corrective-action separation pass | [Buildout 13](PRODUCT_ROADMAP.md#buildout-13--diagnostics-and-failure-explanation) |
| [ ] | 25 | Expand engineering operations through the capability-admission template | 24 checked; repeat per capability | Each admitted operation passes its own specification and required tests | [Buildout 14](PRODUCT_ROADMAP.md#buildout-14--engineering-capability-expansion) |
| [ ] | 26 | Harden reliability, security, storage, and recovery | 22 checked; repeat after later capabilities | Buildout 15 failure-closed and operational-recovery gates pass | [Buildout 15](PRODUCT_ROADMAP.md#buildout-15--reliability-security-and-operational-hardening) |
| [ ] | 27 | Package the supported environment and add versions deliberately | Foundation stable; 26 checked | Fresh-machine probe and every claimed compatibility combination pass | [Buildout 16](PRODUCT_ROADMAP.md#buildout-16--packaging-and-multi-version-support) |
| [ ] | 28 | Add optional clients without duplicating core behavior | 27 checked | Client-invariance and no-direct-PowerFactory-access gates pass | [Buildout 17](PRODUCT_ROADMAP.md#buildout-17--optional-client-ecosystem) |
| [ ] | 29 | Verify the complete product foundation end to end | 1–27 checked; 28 optional | Every product-foundation completion criterion is demonstrated and evidenced | [Complete Product Foundation](PRODUCT_ROADMAP.md#16-definition-of-the-complete-product-foundation) |

Buildout 0 code preparation exists, but item 5 stays unchecked: **BLOCKED — Windows validation required**.
