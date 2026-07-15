# Open-Source Adoption Ledger

This ledger records candidate upstream patterns, code adaptations, direct dependencies, and rejected approaches for the product. Upstream behavior is research evidence, not product acceptance evidence. An entry below is **not adopted** until its validation plan passes and a later delivery decision records that evidence.

## Inspected revisions

| Repository | Local clone | Inspected commit | Licence | Source graph |
|---|---|---|---|---|
| [PowerMCP](https://github.com/Power-Agent/PowerMCP) | `../PowerMCP` | `52deb675d3a83fd63948a18175158590622cc5ef` | [MIT](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/LICENSE) | `../PowerMCP/graphify-out/graph.json` |
| [`powerfactory-tools`](https://github.com/ieeh-tu-dresden/powerfactory-tools) | `../powerfactory-tools` | `89adb7bf390912652201d0819395bdd0e8150688` | [BSD-3-Clause](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/LICENSE) | `../powerfactory-tools/graphify-out/graph.json` |

Both working trees were clean at inspection time and matched the commits above. Graph results were used only to locate candidates; every conclusion below was checked against the pinned source and the named upstream tests or explicitly marked as lacking a focused upstream test.

## Decision classifications

- **Learned pattern:** reimplement the idea behind the product's own contracts.
- **Adapted code:** modify licensed upstream source and preserve the applicable notice and provenance.
- **Direct dependency:** import the upstream package behind a product-owned adapter and retain its distributed licence notices.
- **Rejected approach:** explicitly prohibit an upstream behavior or boundary.

## PowerMCP candidate decisions

### PM-01 — Serialize all PowerFactory API calls and initialize the vendor layer lazily

- **Upstream:** PowerMCP @ `52deb675d3a83fd63948a18175158590622cc5ef` — MIT.
- **Source/test anchors:** [`PowerFactory/MCP_PowerFactory.py::_pf`, `_load_modules`, `_pf_executor`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/MCP_PowerFactory.py#L94-L111); [`PowerFactory/Agent_DIgSILENT.py::DIgSILENTAgent.connect`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/Agent_DIgSILENT.py#L256-L296). Graph path: `_pf() <--calls-- modify_parameter()`, with all calculation/import tools also adjacent to `_pf()`. No PowerFactory-specific executor or thread-affinity test exists in the pinned `PowerFactory/` or `tests/` trees.
- **Classification/status:** **Learned pattern** — adopted for the platform-independent serialized owner; native PowerFactory acceptance remains deferred.
- **Destination:** Buildout 2 serialized PowerFactory gateway executor and lazy application/session lifecycle boundary.
- **Product contract/rationale:** one owned execution lane must preserve vendor thread affinity and prevent concurrent engine entry; importing the product or starting MCP must not require PowerFactory to be installed or running.
- **Upstream assumptions to remove:** process-global executor and application handles, unbounded `Future.result()`, no queue admission limit, no operation deadline/cancellation semantics, and the assumption that `GetApplicationExt()` attaches to the desired session.
- **Validation plan:** fake-gateway tests submit concurrent calls and assert FIFO execution on one stable thread, bounded queue rejection, timeout propagation, and no vendor import before first gateway call; the Windows PowerFactory 2026 probe repeats start/use/close and records thread IDs, process identity, attach/start behavior, and recovery after a failed call.
- **Preparation evidence:** `SerializedOperationWorker` uses one bounded FIFO lane and stable handler thread, durable idempotent operation records, separate queue/client/health deadlines, non-cancelling client timeout, quarantine, and restart reconciliation. Local concurrency, timeout, late-result, and recovery tests pass. Native thread affinity, lazy vendor startup, and thread-versus-process acceptance remain `BLOCKED — Windows validation required`.

### PM-02 — Reuse the stdio launcher and idempotent local client-configuration pattern

- **Upstream:** PowerMCP @ `52deb675d3a83fd63948a18175158590622cc5ef` — MIT.
- **Source/test anchors:** [`PowerFactory/MCP_PowerFactory.py::mcp`, `_default_cfg_path`, __main__`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/MCP_PowerFactory.py#L54-L89), [`mcp.run`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/MCP_PowerFactory.py#L613-L623), [`powermcp/registry.py` PowerFactory entry](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/powermcp/registry.py#L170-L185), [`powermcp/runner.py::launch`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/powermcp/runner.py#L45-L88), and [`powermcp/clients/_common.py::server_entry`, `merge_mcp_servers`, `write_json_config`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/powermcp/clients/_common.py#L31-L85). Upstream tests cover absolute-interpreter entries, idempotent merge/backup/dry-run, Windows-only preflight, registry paths, and generic stdio launch in [`tests/test_clients.py`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/tests/test_clients.py#L11-L92), [`tests/test_runner.py`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/tests/test_runner.py#L17-L92), and [`tests/test_registry.py`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/tests/test_registry.py#L32-L48); they do not launch the PowerFactory server.
- **Classification/status:** **Learned pattern** — candidate, not adopted.
- **Destination:** Buildout 11 thin authenticated MCP adapter and deployment/client configuration tooling.
- **Product contract/rationale:** generated client entries should use an absolute interpreter, stdio transport, atomic writes, managed-key pruning, foreign-entry preservation, and dry-run output while keeping business logic outside MCP.
- **Upstream assumptions to remove:** direct execution of a bundled monolithic server, unauthenticated local access, mutable config copying during first tool use, support for arbitrary transport selection, and client configuration as the source of authorization.
- **Validation plan:** schema-level MCP tests assert only admitted tools are exposed; installer tests cover atomic update, backup, dry-run, idempotency, foreign-entry preservation, secret-free output, and Windows paths; Codex plus a second MCP client must complete authenticated health/read workflows while direct unauthenticated calls fail closed.

### PM-03 — Sanitize non-native and non-finite values at the schema boundary

- **Upstream:** PowerMCP @ `52deb675d3a83fd63948a18175158590622cc5ef` — MIT.
- **Source/test anchors:** [`PowerFactory/MCP_PowerFactory.py::_to_json`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/MCP_PowerFactory.py#L113-L143), which recursively converts NumPy arrays/scalars, stringifies mapping keys, converts tuples to arrays, and maps `NaN`/infinity to `null`. No focused upstream serialization test was found.
- **Classification/status:** **Learned pattern** — adopted through a product-owned strict serializer; no upstream code was copied.
- **Destination:** Buildout 1 typed domain/schema serialization helpers, before Buildout 11 MCP response encoding.
- **Product contract/rationale:** generated JSON schemas must produce deterministic JSON-compatible values and an explicit policy for non-finite engine results.
- **Upstream assumptions to remove:** returning pre-encoded JSON strings from tools, silently replacing invalid numbers without field-level diagnostics, optional NumPy behavior changing output shape, unrestricted payload size, and accepting arbitrary objects through a recursive fallback.
- **Validation plan:** property and golden tests cover nested typed models, NumPy scalars/arrays when installed, tuples, non-string keys, `NaN`/±infinity policy, deterministic ordering, payload limits, and round-trip validation against generated response schemas; MCP tests assert one encoding layer only.
- **Preparation evidence:** canonical JSON and generated-schema tests cover admitted typed models, deterministic ordering, Unicode normalization, UTC timestamps, typed digests, payload bounds, and rejection of arbitrary or non-finite values. NumPy is not an admitted public dependency, and MCP one-layer encoding remains a Buildout 11 gate.

### PM-04 — Model project import/export and study-case activation as explicit workflows

- **Upstream:** PowerMCP @ `52deb675d3a83fd63948a18175158590622cc5ef` — MIT.
- **Source/test anchors:** [`DIgSILENTAgent.import_project`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/Agent_DIgSILENT.py#L738-L790), [`export_project_to_pfd`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/Agent_DIgSILENT.py#L554-L585), [`activate_study_case`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/Agent_DIgSILENT.py#L300-L349), and [`create_study_case`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/Agent_DIgSILENT.py#L1103-L1205). The MCP wrapper adds only a one-process, one-hour `request_id` cache. No focused upstream tests cover these operations.
- **Classification/status:** **Learned pattern** — candidate, not adopted.
- **Destination:** Buildout 2 gateway primitives plus Buildouts 6 and 10 durable project/study-case workflow steps.
- **Product contract/rationale:** import/export and activation must be named, authorized, auditable operations with explicit pre-state, post-state, ownership, idempotency, and compensation evidence.
- **Upstream assumptions to remove:** caller-controlled absolute paths, immediate activation after import, copying a hard-coded `0. Base`, in-memory idempotency, GUI defaults, shared mutable project handles, and success represented only by `(bool, str)`.
- **Validation plan:** fake-gateway contract tests cover missing files/cases, exact-name ambiguity, idempotent replay across restart, activation mismatch, import failure, export collision, and bounded paths; real fixture tests capture project/study-case identity before and after each operation and prove compensation or an explicit non-reversible decision.

### PM-05 — Turn the IEEE 39-bus walkthrough and install friction into acceptance probes

- **Upstream:** PowerMCP @ `52deb675d3a83fd63948a18175158590622cc5ef` — MIT.
- **Source/test anchors:** [`PowerFactory/README.md` workflow and IEEE39 prompt](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/README.md#L149-L200), [`PowerFactory/INSTALL.txt`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/INSTALL.txt#L1-L103), [`PowerFactory/IEEE39.pfd`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/IEEE39.pfd), and [`simulation_config.example.json`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/simulation_config.example.json). These are example/manual assets, not automated tests.
- **Classification/status:** **Learned pattern** — candidate, not adopted; the binary `.pfd` is not copied unless fixture provenance and redistribution are separately accepted.
- **Destination:** Buildout 0 environment/API capability proof and Buildout 16 fresh-machine packaging gate.
- **Product contract/rationale:** a known grid workflow should expose Python ABI mismatch, vendor-module discovery, licence availability, project import, active-case selection, calculation, and result retrieval failures as structured probe evidence.
- **Upstream assumptions to remove:** `pip`/manual environment setup, globally edited `PYTHONPATH`, pre-started GUI, hand-copied config, generic “PowerFactory 2023 or later” compatibility, and success verified by human prompts.
- **Validation plan:** using `uv`, provision the supported Windows environment from lock data; run a non-destructive PowerFactory 2026 probe against an approved fixture; record executable/module/API versions, service pack, licence mode, process ownership, project/study-case identity, load-flow status, and cleanup; rerun from a fresh machine image and deliberately test missing module, wrong ABI, missing licence, and absent fixture.

### PM-06 — Separate calculation command execution from typed result collection

- **Upstream:** PowerMCP @ `52deb675d3a83fd63948a18175158590622cc5ef` — MIT.
- **Source/test anchors:** [`MCP_PowerFactory.py::run_loadflow`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/MCP_PowerFactory.py#L291-L339), [`DIgSILENTAgent.load_flow` and `_export_loadflow_snapshot_to_csv`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/Agent_DIgSILENT.py#L904-L1063), [`export_results_to_csv`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/Agent_DIgSILENT.py#L517-L552), [`run_pipeline`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/Agent_DIgSILENT.py#L1207-L1270), and [`MCP_PowerFactory.py::read_results_csv`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/MCP_PowerFactory.py#L494-L599). No PowerFactory-specific calculation/result test was found.
- **Classification/status:** **Learned pattern** — adopted in the vendor-primitive command/result boundary; native PowerFactory acceptance remains deferred.
- **Destination:** Buildout 5 calculation service, immutable result store, result overlays, and gateway command/result adapters.
- **Product contract/rationale:** command configuration/execution, convergence evidence, result extraction, provenance, and presentation must be separate typed steps; files may be evidence artifacts but not canonical workflow state.
- **Upstream assumptions to remove:** active study case as implicit input, default `ComLdf` settings, fixed attribute lists, CSV/latest-file discovery, timestamps as identity, swallowed attribute-read errors, free-form status strings, and a monolithic RMS pipeline.
- **Validation plan:** fake and real gateway tests assert exact command attributes, nonzero `Execute()` handling, immutable calculation IDs, active-context/revision binding, deterministic typed extraction, unsupported-variable diagnostics, row/payload bounds, restart-safe retrieval, and equivalence between stored raw evidence and derived overlays.
- **Preparation evidence:** the primitive gateway exposes separate typed `execute_command` and `collect_results` calls. Result cells explicitly distinguish available, missing, unsupported, and non-finite values while preserving source evidence and normalized quantities. Immutable calculation storage, overlays, and native command/result parity remain Buildout 5 work.

### PM-R01 — Reject immediate unrestricted `modify_parameter` writes

- **Upstream:** PowerMCP @ `52deb675d3a83fd63948a18175158590622cc5ef` — MIT.
- **Source/test anchors:** [`MCP_PowerFactory.py::modify_parameter`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/MCP_PowerFactory.py#L259-L289) directly calls [`DIgSILENTAgent.modify_parameter`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/Agent_DIgSILENT.py#L796-L900), which queries arbitrary objects, coerces from current values, and loops over `SetAttribute` without approval, preview, revision check, rollback, or per-object atomicity. The README advertises this unrestricted surface; no mutation-safety test was found.
- **Classification/status:** **Rejected approach** — prohibited product boundary.
- **Destination:** Buildouts 6–10 safety/workflow boundary; there will be no generic MCP write tool or generic gateway mutation endpoint.
- **Product contract/rationale:** writes require an admitted capability, bounded target set, typed preview, authorization, context lease, expected revision, durable audit, and a selected restoration strategy.
- **Upstream assumptions to remove:** arbitrary query and attribute names, multi-object immediate writes, truthy/string coercion, GUI side effects, and partial success hidden behind one boolean.
- **Validation plan:** surface-enumeration tests prove no generic write tool is exposed; adversarial tests reject wildcard targets, unknown attributes, forged/replayed approvals, stale revisions, and over-broad selections; failure injection proves no write occurs before authorization and that partial application is detected and reconciled.

### PM-R02 — Reject GUI-dependent lifecycle behavior

- **Upstream:** PowerMCP @ `52deb675d3a83fd63948a18175158590622cc5ef` — MIT.
- **Source/test anchors:** [`DIgSILENTAgent._apply_show_preference`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/Agent_DIgSILENT.py#L181-L194) retries `app.Show()` five times; most MCP tools default `open_digsilent=True`; [`INSTALL.txt`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/INSTALL.txt#L74-L101) requires users to start and keep the GUI running. No headless lifecycle test was found.
- **Classification/status:** **Rejected approach** — prohibited runtime assumption.
- **Destination:** Buildouts 0, 2, and 16 deployment/process-ownership contract.
- **Product contract/rationale:** the supported external-engine mode must have explicit process ownership and work without GUI interaction; showing a window is an optional operator action, never a correctness prerequisite.
- **Upstream assumptions to remove:** pre-existing desktop session, interactive licence/UI access, `Show()` retries as readiness, and unconditional `Exit()` of a process the product may not own.
- **Validation plan:** run lifecycle probes in the supported non-interactive Windows environment; assert no `Show()` call in normal operation; test attach-owned and product-owned process modes separately; verify close affects only product-owned sessions and records actionable readiness/licence errors.

### PM-R03 — Reject the monolithic simulation wrapper

- **Upstream:** PowerMCP @ `52deb675d3a83fd63948a18175158590622cc5ef` — MIT.
- **Source/test anchors:** [`DIgSILENTAgent.run_pipeline`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/Agent_DIgSILENT.py#L1207-L1270) chains connect, case activation, load flow, RMS simulation, CSV, plots, and optional PFD export; [`MCP_PowerFactory.py::run_simulation`, `run_custom_case`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/MCP_PowerFactory.py#L361-L493) expose that chain directly. No step-resume, crash-recovery, or orchestration test was found.
- **Classification/status:** **Rejected approach** — prohibited architecture.
- **Destination:** Buildouts 5, 6, and 10 calculation and durable workflow orchestration.
- **Product contract/rationale:** independently testable gateway primitives must be composed by a persistent state machine with explicit checkpoints, immutable evidence, retry classification, and compensation.
- **Upstream assumptions to remove:** in-memory sequential control, stop-on-first-error as recovery, filesystem paths as workflow state, optional plotting inside the critical path, and one report dictionary as history.
- **Validation plan:** workflow tests restart after every transition, replay idempotent steps, inject gateway and persistence failures, distinguish retryable/terminal outcomes, and prove that calculation, export, plotting, and rollback can be resumed or compensated independently.

### PM-R04 — Reject the absence of independent authorization, rollback, context persistence, and typed history

- **Upstream:** PowerMCP @ `52deb675d3a83fd63948a18175158590622cc5ef` — MIT.
- **Source/test anchors:** the complete PowerFactory MCP surface is declared in [`MCP_PowerFactory.py`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/MCP_PowerFactory.py#L145-L623); shared state and the only idempotency cache are in [`Agent_DIgSILENT.py::DIgSILENTAgent`](https://github.com/Power-Agent/PowerMCP/blob/52deb675d3a83fd63948a18175158590622cc5ef/PowerFactory/Agent_DIgSILENT.py#L166-L178). At the pinned tree, source/test searches found no PowerFactory authorization, approval, rollback, lease, audit, persistent context, transaction, or typed workflow-history implementation.
- **Classification/status:** **Rejected approach** — absence is an explicit non-inheritance decision.
- **Destination:** Buildout 6 durable workflow/authorization/audit foundation and all mutating capabilities.
- **Product contract/rationale:** MCP reachability and an in-process request cache are not authorization or durability; all state-changing work must survive restart and remain attributable and reconcilable.
- **Upstream assumptions to remove:** trusted local caller, process lifetime as context lifetime, mutable class variables as idempotency, tool return text as audit, and no crash boundary between write and response.
- **Validation plan:** restart, forgery, replay, lease-expiry, stale-context, crash-after-write, and audit-chain tests must pass before any mutation tool is admitted; product schemas must expose typed workflow events and reconciliation outcomes without relying on MCP process memory.

## `powerfactory-tools` candidate decisions

### PFT-01 — Learn exact external-engine startup and version/service-pack selection

- **Upstream:** `powerfactory-tools` @ `89adb7bf390912652201d0819395bdd0e8150688` — BSD-3-Clause.
- **Source/test anchors:** [`pf2026/interface.py::PowerFactoryInterface.__post_init__`, `load_powerfactory_module_from_path`, `connect_to_app`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L105-L243) checks the selected CPython major/minor, loads `powerfactory.pyd` from either `PowerFactory 2026` or `PowerFactory 2026 SP<n>`, optionally builds `/ini` arguments, and calls `GetApplicationExt(profile, password, command_line_arg)`. Separate interfaces/types exist at exact paths under [`pf2022`](https://github.com/ieeh-tu-dresden/powerfactory-tools/tree/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2022), [`pf2024`](https://github.com/ieeh-tu-dresden/powerfactory-tools/tree/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2024), [`pf2025`](https://github.com/ieeh-tu-dresden/powerfactory-tools/tree/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2025), and [`pf2026`](https://github.com/ieeh-tu-dresden/powerfactory-tools/tree/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026). [`tests/test_interface.py`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/tests/test_interface.py#L6-L64) mocks module loading, connection, and project join, so it proves only constructor/context flow and log messages—not real startup.
- **Classification/status:** **Learned pattern** — candidate, not adopted.
- **Destination:** Buildout 0 capability probe and Buildout 2 PowerFactory 2026 gateway lifecycle adapter.
- **Product contract/rationale:** startup evidence must bind exact main version, service pack, Python ABI, module path, ini/database, user profile, process identity, and licence mode before the gateway is declared ready.
- **Upstream assumptions to remove:** fixed `C:\Program Files\DIgSILENT`, loading a `.pyd` directly as the only supported discovery path, constructor side effects, credentials passed through ordinary config fields, and treating each copied version module as product compatibility proof.
- **Validation plan:** a matrix probe exercises supported PowerFactory 2026 service packs and selected Python ABI, records module/application versions and process identity, tests wrong ABI/path/ini/profile/licence failures, and fails closed on any unclaimed combination; no later version is advertised without the same evidence.

### PFT-02 — Use typed object-query wrappers, not raw query strings at product boundaries

- **Upstream:** `powerfactory-tools` @ `89adb7bf390912652201d0819395bdd0e8150688` — BSD-3-Clause.
- **Source/test anchors:** [`pf2026/types.py::PFClassId`, `FolderType`, protocols`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/types.py#L17-L125), [`pf2026/interface.py::grid_elements`, `first_of`, `elements_of`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L1782-L1910), and typed methods such as [`terminals`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L1294-L1317) and [`loads`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L1590-L1613). No focused query/typing behavior test exists in the pinned tests.
- **Classification/status:** **Learned pattern** — adopted in the typed primitive-query and bounded inventory contracts; native class mapping remains deferred.
- **Destination:** Buildout 1 typed domain contracts and Buildouts 2–3 gateway inventory/query adapters.
- **Product contract/rationale:** callers select admitted object kinds and bounded filters; only the gateway translates those types into PowerFactory class IDs and query calls.
- **Upstream assumptions to remove:** wildcard-by-default APIs, `first_of` silently selecting one ambiguous match, list materialization without pagination, runtime casts as validation, active-grid global state, and raw `DataObject` escape across the gateway.
- **Validation plan:** fake and real contract tests cover exact class mapping, ambiguity errors, unsupported classes, active/out-of-service semantics, deterministic ordering, pagination/limits, bounded traversal, and conversion to owned DTOs with no vendor object leakage.
- **Preparation evidence:** public primitive queries use admitted, versioned class/attribute/relationship selectors with explicit scope, out-of-service policy, limits, cursor binding, completeness, truncation, and warnings. The inventory service returns owned DTOs and fails closed on ambiguous project or product identity evidence. Exact PowerFactory class translation remains `BLOCKED — Windows validation required`.

### PFT-03 — Preserve and restore project unit settings around normalized reads

- **Upstream:** `powerfactory-tools` @ `89adb7bf390912652201d0819395bdd0e8150688` — BSD-3-Clause.
- **Source/test anchors:** [`join_project`, `release_project`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L246-L293), [`set_default_unit_conversion`, `stash_unit_conversion_settings`, `pop_unit_conversion_settings_stash`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L776-L843), and [`BaseUnits.UNITCONVERSIONS`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/constants.py#L44-L67). Graph path: `stash_unit_conversion_settings -> delete_unit_conversion_settings <- pop_unit_conversion_settings_stash`. Upstream has no unit-stash/restoration test.
- **Classification/status:** **Adapted code** — candidate; copied state-shape/constants require BSD-3-Clause notice and source provenance.
- **Destination:** Buildout 2 gateway unit context and Buildouts 3/5 inventory/calculation normalization.
- **Product contract/rationale:** every numeric field needs declared source and canonical units, and any temporary project setting change must restore exactly even on error or cancellation.
- **Upstream assumptions to remove:** destructive delete/recreate as an unguarded setup step, reset by project deactivate/reactivate, one in-memory stash, suppression of restoration errors, no revision/ownership check, and normalization coupled to joining a project.
- **Validation plan:** snapshot all project/unit conversion settings, enter the normalization context, verify canonical units and extracted values, then compare byte-for-byte semantic settings after success, exception, timeout, cancellation, and process crash/reconciliation; nested/concurrent contexts must be rejected or isolated; real tests record before/after project revision.

### PFT-04 — Wrap project, study-case, scenario, and variant state changes as explicit context operations

- **Upstream:** `powerfactory-tools` @ `89adb7bf390912652201d0819395bdd0e8150688` — BSD-3-Clause.
- **Source/test anchors:** [`switch_study_case`, `switch_scenario`, `switch_grid_variant`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L429-L461), [`activate_scenario`, `activate_study_case`, `activate_grid_variant`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L581-L731), and query methods [`study_cases`, `scenarios`, `grid_variants`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L993-L1041). The pinned tests do not exercise these operations.
- **Classification/status:** **Learned pattern** — candidate, not adopted.
- **Destination:** Buildout 2 gateway context primitives, Buildout 6 context leases, and Buildouts 9–10 scenario-isolation/application workflows.
- **Product contract/rationale:** context activation/deactivation must be typed, identity-checked, lease-bound, auditable, and reversible without relying on ambient active state.
- **Upstream assumptions to remove:** names as sufficient identity, deactivating all variants before selection, automatically activating the first variant stage, no expected-revision check, and no restoration of the previously active context after failure.
- **Validation plan:** capture project/case/scenario/variant/stage identities before each operation; test exact selection, duplicate names, stale leases, nested activation, stage ordering, exception cleanup, restart reconciliation, and restoration of the prior context; prove scenario-isolation leakage tests before selecting that mutation strategy.

### PFT-05 — Evaluate the PSDM exporter as a dependency behind an owned export adapter

- **Upstream:** `powerfactory-tools` @ `89adb7bf390912652201d0819395bdd0e8150688` — BSD-3-Clause.
- **Source/test anchors:** [`PowerFactoryExporter.export`, `export_study_cases`, `export_active_study_case`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/exporter/exporter.py#L209-L376), [`create_topology`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/exporter/exporter.py#L510-L552), [`create_topology_case`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/exporter/exporter.py#L2155-L2220), and [`create_steadystate_case`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/exporter/exporter.py#L2578-L2615). [`tests/test_schema.py`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/tests/test_schema.py#L19-L45) round-trips checked-in topology/topology-case/steady-state JSON fixtures, but does not run PowerFactory extraction or verify product graph identity.
- **Classification/status:** **Direct dependency** — evaluation candidate only; no dependency decision has been made.
- **Destination:** optional Buildout 3 export adapter and Buildout 4 graph-ingest/projection boundary, never the product's authoritative domain or identity model.
- **Product contract/rationale:** the exporter offers a typed separation of assets, switching/out-of-service state, and operating points plus topology-match checks; using the package is safer than copying its multi-thousand-line conversion implementation.
- **Upstream assumptions to remove:** passive sign convention as automatically product-compatible, supported-element subset as complete, active study case/grids as implicit scope, `do_not_export` description markers, filenames/timestamps as identity, and PSDM match as proof of product revision/identity correctness.
- **Validation plan:** pin the dependency and transitive `ieeh-power-system-data-model` version in `uv.lock`; run approved PowerFactory 2026 fixtures through the adapter; validate supported/unsupported element accounting, sign/unit policy, stable product IDs, topology/topology-case/steady-state referential integrity, deterministic output, and explicit mismatch diagnostics; compare against checked-in upstream fixtures without making them product acceptance evidence.

### PFT-R01 — Reject upstream generated names as durable product identity

- **Upstream:** `powerfactory-tools` @ `89adb7bf390912652201d0819395bdd0e8150688` — BSD-3-Clause.
- **Source/test anchors:** [`pf_dataobject_to_name_string`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L2912-L2916) uses `GetFullName()` or delegates to [`create_name`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L3002-L3040), which constructs names from `loc_name` and selected parent/substation names. No stability/collision/rename test exists upstream.
- **Classification/status:** **Rejected approach** for authoritative identity; retained only as a learned display/export-label pattern.
- **Destination:** Buildout 3 display-name field and Buildout 4 identity/revision mapping; never a database primary key, workflow target ID, or authorization subject.
- **Product contract/rationale:** durable identity must survive display-name changes and distinguish same-named objects across projects, grids, folders, classes, and revisions.
- **Upstream assumptions to remove:** path/name uniqueness, stable parent hierarchy, grid-relative names as globally unique, and appending class names as sufficient identity.
- **Validation plan:** identity tests include duplicate names, rename/move, copied study cases, project re-import, multiple grids/classes, and restart; assert stable product IDs where vendor identity supports it, explicit tombstone/rebind behavior otherwise, and preservation of upstream names only as provenance/display aliases.

### PFT-06 — Adapt typed load-flow command configuration and explicit execution checks

- **Upstream:** `powerfactory-tools` @ `89adb7bf390912652201d0819395bdd0e8150688` — BSD-3-Clause.
- **Source/test anchors:** [`pf2026/interface.py::create_ldf_command`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L2397-L2430) maps AC/DC and symmetrical/unsymmetrical choices to typed enums and permits additional attributes; [`run_ldf`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L2607-L2632) treats nonzero `Execute()` as failure and returns the default `All*` result. No focused load-flow command test exists upstream.
- **Classification/status:** **Adapted code** — candidate; copied enum/configuration logic requires BSD-3-Clause notice and provenance.
- **Destination:** Buildout 5 load-flow gateway command builder, accepted `load-flow-and-violation-policy/v1`, and typed result collector.
- **Product contract/rationale:** calculation settings must be explicit, versioned, validated, and recorded with the immutable result; engine return codes must become typed failures.
- **Upstream assumptions to remove:** only three network-mode choices, arbitrary `data` attributes, active study case as implicit context, truthiness as the full error model, and `All*` first-match result selection.
- **Validation plan:** command-spy tests assert every supported field for each admitted mode and reject unknown attributes; real tests record command attributes, return code, convergence/status evidence, selected result object identity, context revision, deterministic extraction, and policy-derived violations; unsupported modes fail before engine execution.

### PFT-07 — Use context-managed cleanup while preserving product process ownership

- **Upstream:** `powerfactory-tools` @ `89adb7bf390912652201d0819395bdd0e8150688` — BSD-3-Clause.
- **Source/test anchors:** [`PowerFactoryInterface.__enter__`, `__exit__`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L160-L169), [`release_project`, `close`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L278-L293), [`close`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L384-L393), and exporter context cleanup in [`exporter.py`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/exporter/exporter.py#L237-L246). [`tests/test_interface.py`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/tests/test_interface.py#L6-L64) verifies enter/exit logging with module, application, and project operations mocked; it does not assert restoration or `PostCommand("exit")`.
- **Classification/status:** **Learned pattern** — candidate, not adopted.
- **Destination:** Buildout 2 gateway session resource manager and Buildout 15 recovery/cleanup handling.
- **Product contract/rationale:** gateway resources need structured cleanup on success and exception, with restoration attempted before process termination and every cleanup failure recorded.
- **Upstream assumptions to remove:** `close()` may always issue `PostCommand("exit")`, suppressed `AttributeError` is sufficient cleanup reporting, project/unit restoration can fail silently, and synchronous `__exit__` covers timeout/process-crash cases.
- **Validation plan:** ownership-mode tests assert attached processes are never exited, owned processes are terminated exactly once, project/unit context restores on exceptions, cleanup failures remain visible, repeated close is idempotent, and startup/operation crashes are reconciled on restart.

### PFT-R02 — Reject constructor side effects and swallowed startup failures

- **Upstream:** `powerfactory-tools` @ `89adb7bf390912652201d0819395bdd0e8150688` — BSD-3-Clause.
- **Source/test anchors:** [`PowerFactoryInterface.__post_init__`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/src/powerfactory_tools/versions/pf2026/interface.py#L118-L137) loads the vendor module, connects, optionally joins a project, catches `RuntimeError`, logs, and calls `close()` without re-raising; [`tests/test_interface.py`](https://github.com/ieeh-tu-dresden/powerfactory-tools/blob/89adb7bf390912652201d0819395bdd0e8150688/tests/test_interface.py#L6-L64) mocks those failure-prone operations and notes that some error assertions cannot be collected.
- **Classification/status:** **Rejected approach** — prohibited gateway lifecycle behavior.
- **Destination:** Buildouts 0 and 2 startup/readiness state machine.
- **Product contract/rationale:** construction must be side-effect free; explicit `start()`/`connect()` must either return typed capability evidence or raise a typed failure, never leave a partially initialized object that appears usable.
- **Upstream assumptions to remove:** logging as error propagation, `close()` on partially initialized attributes, immediate project mutation during construction, and readiness inferred from object creation.
- **Validation plan:** tests inject failure at module load, application connect, project join, unit stash/normalization, and folder discovery; each must produce a typed failure, clean only acquired resources, expose no ready gateway, and leave enough evidence for retry/reconciliation.

## Roadmap extraction coverage

| Roadmap extraction target | Ledger mapping | Verification status |
|---|---|---|
| PowerMCP dedicated `ThreadPoolExecutor(max_workers=1)` | PM-01 | Graph `_pf` neighborhood/path plus pinned source; no focused upstream test. |
| PowerMCP lazy application initialization | PM-01 | Deferred server import and first-call `GetApplicationExt()` verified in pinned source; no focused upstream test. |
| PowerMCP MCP startup and local configuration | PM-02 | Pinned server, registry, runner, client writers, and upstream launcher/config tests verified. |
| PowerMCP serialization and JSON sanitization | PM-03 | `_to_json` verified; no focused upstream test. |
| PowerMCP import/export and study-case handling | PM-04 | Exact agent methods verified; no focused upstream test. |
| PowerMCP IEEE example workflow and installation friction | PM-05 | README, install guide, config example, and tracked `IEEE39.pfd` verified; manual evidence only. |
| PowerMCP calculation execution and result collection | PM-06 | `ComLdf`, CSV extraction/export, pipeline, and result reader verified; no focused upstream test. |
| Reject unrestricted `modify_parameter` | PM-R01 | One-hop graph path and direct `SetAttribute` loop verified; no safety test. |
| Reject GUI-dependent lifecycle assumptions | PM-R02 | `Show()` retries, GUI-default tool parameters, and install instructions verified. |
| Reject monolithic simulation wrapper | PM-R03 | MCP-to-`run_pipeline` composition verified; no resume/recovery test. |
| Reject missing authorization, rollback, context persistence, and typed history | PM-R04 | Complete PowerFactory surface/state inspected; named capabilities absent from pinned source/tests. |
| `powerfactory-tools` external-engine startup behavior | PFT-01 | `powerfactory.pyd` load, ABI check, ini construction, and `GetApplicationExt` verified; real startup untested upstream. |
| Release- and service-pack-specific interfaces | PFT-01 | Separate `pf2022`/`pf2024`/`pf2025`/`pf2026` modules and `SP<n>` path branch verified. |
| Object query and typing patterns | PFT-02 | Typed protocols/enums and query wrappers verified; no focused upstream test. |
| Unit normalization and restoration | PFT-03 | Graph stash/pop path and exact stash/delete/recreate/reset source verified; no focused upstream test. |
| Project, study-case, scenario, and variant operations | PFT-04 | Exact switch/activate/query methods verified; no focused upstream test. |
| PSDM topology, topology-case, and steady-state export | PFT-05 | Graph export path, exporter source, match checks, fixtures, and schema round-trip tests verified. |
| Stable naming and object identity assumptions | PFT-R01 | Full-name/relative-name algorithms verified; no stability test; rejected as durable identity. |
| Load-flow command configuration | PFT-06 | `create_ldf_command` and `run_ldf` verified; no focused upstream test. |
| Error handling and context-manager cleanup | PFT-07 and PFT-R02 | Context/close/release/startup paths and mocked context test verified; ownership/restoration remain locally unproven. |

**Unverified extraction targets:** none. “No focused upstream test” means the implementation was located and inspected, but the product must supply the listed contract test before adoption.
