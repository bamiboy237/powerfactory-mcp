## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

## Project execution sources

Use the project documents for distinct purposes:

- `PRODUCT_ROADMAP.md` defines the overall product vision, architecture, and buildout intent.
- `IMPLEMENTATION_CHECKLIST.md` is the compact execution index and completion record. Start with the first unchecked item whose prerequisites are satisfied, unless preparing non-accepted work for an asynchronous Windows handoff as described below.
- `specs/development/open-source-adoption-ledger.md` records upstream patterns considered, adopted, adapted, or rejected. Consult it before reimplementing equivalent PowerFactory or MCP behavior.
- `specs/delivery/buildout-dependency-matrix.md` defines the evidence required to accept each buildout. Code existing is not completion; the row's commands and decision gate must pass.
- Companion specifications under `specs/` define executable behavior. Do not invent behavior that belongs to a required but unaccepted specification.

Open only the checklist-selected roadmap section and relevant specifications. Use `rg`, Graphify, and source links instead of loading the entire roadmap into agent context.

## Distributed macOS and Windows workflow

Development is asynchronous across two environments:

- The primary developer and coding agents work on macOS without DIgSILENT PowerFactory.
- The Windows teammate has PowerFactory and owns real-engine execution, compatibility probes, and PowerFactory-dependent validation.
- Work is shipped through Git. Every Windows result must be tied to the exact tested commit SHA; feedback against another revision is not acceptance evidence.

### macOS responsibilities

On macOS, agents should:

1. implement platform-independent domain logic, schemas, persistence, workflow orchestration, graph projections, and MCP behavior behind explicit interfaces;
2. use the fake gateway and deterministic fixtures for unit, contract, failure-injection, and recovery tests;
3. keep all `powerfactory` imports inside the Windows gateway boundary so the core package and tests remain usable without PowerFactory;
4. prepare deterministic Windows probes, integration tests, commands, and sanitized evidence templates required by the dependency matrix;
5. run all available local checks before shipping a commit for Windows validation;
6. report PowerFactory-dependent checks as `BLOCKED — Windows validation required`, never as passed or failed merely because PowerFactory is unavailable;
7. continue preparing later platform-independent work only when it does not require inventing results, accepting evidence-dependent specifications, or bypassing safety gates.

Preparation order may run ahead to keep development productive, but acceptance order does not change. Do not check a checklist item or enable dependent live behavior until its prerequisite specifications and dependency-matrix gates are satisfied.

### Windows teammate responsibilities

The Windows handoff must ask the teammate to:

1. check out the exact commit SHA being evaluated;
2. record the PowerFactory release, service pack, licence capability, `powerfactory.pyd` path, compatible CPython major/minor and architecture, and non-confidential fixture identifier where relevant;
3. run the exact dependency-matrix commands without silently modifying them;
4. return sanitized command output, exit codes, logs, generated evidence artifacts, and engineering observations;
5. report manual deviations, GUI interaction, stale processes, licence failures, crashes, hangs, or fixture changes explicitly;
6. avoid committing confidential models, credentials, licence data, or unsanitized customer results.

### Feedback and acceptance loop

For every PowerFactory-dependent buildout:

```text
macOS implementation and fake/contract tests
→ push an exact commit
→ Windows teammate runs real-PowerFactory gates
→ return sanitized evidence and feedback
→ macOS fixes or reconciles findings
→ repeat against a new exact commit when code changed
→ record accepted evidence in the dependency matrix
→ check the implementation item
```

A code change after a Windows run invalidates that run only when it can affect the tested behavior, fixture contract, specification, or compatibility claim. Record the reason when evidence remains valid across a later commit.

Treat teammate feedback as empirical input, not an automatic code instruction. Reproduce or trace the finding, identify whether the cause is product code, environment, PowerFactory behavior, fixture data, or an underspecified contract, and then make the smallest justified change.
