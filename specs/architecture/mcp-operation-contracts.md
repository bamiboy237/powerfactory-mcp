# MCP Operation Contracts

| Field | Value |
| --- | --- |
| Specification key | `MCP` |
| Specification ID | `mcp-operation-contracts/v0.1.0` |
| Status | **PROVISIONAL - Buildout 11 preparation; transport, security, and Windows evidence pending** |
| Governing buildout | Buildout 11 - Thin MCP Surface and Codex Integration |
| Acceptance prerequisite | Accepted Buildout 10, `DEP`, `ID`, `AUTH`, `LEASE`, `WF`, `REC`, and every admitted operation policy/schema |

This specification defines the versioned, machine-readable application
operation contract presented through MCP. It governs the tool catalog, typed
request/response/error envelopes, pagination, timeout and idempotency behavior,
and the MCP authentication boundary. It is not an MCP server implementation,
an approval interface, a PowerFactory gateway contract, or a substitute for an
underlying operation/workflow specification.

The keywords **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative.
Portable fake-gateway protocol work may use this contract. It MUST NOT be
accepted, register execution tools, or claim an authenticated transport until
the Buildout 11 dependency-matrix gates and required client evidence pass.

## 1. Authority And Generated Schema Boundary

The canonical source for every public request, response, nested payload, error,
and tool annotation is a versioned Pydantic domain contract. The build output
MUST generate a deterministic machine-readable schema inventory from those
contracts; handwritten JSON dictionaries, free-form response text, and raw
PowerFactory values are not public tool contracts.

Each generated contract has all of the following:

- a stable operation name and `contract_version` in `major.minor` form;
- a request model, success payload model, and enumerated error model;
- declared operation class, side-effect class, idempotency requirement,
  maximum request/response limits, and pagination capability;
- references to the governing operation/policy and `ID`, `WF`, `LEASE`,
  `AUTH`, and `REC` contracts where they apply; and
- a compatibility declaration identifying supported prior minor versions.

The schema inventory is itself bounded and versioned. Its source digest and
generated artifact digest are recorded in release evidence. An MCP adapter may
translate the MCP protocol wrapper to these models, but it MUST NOT add domain
logic, call PowerFactory, mint identity, infer an approval, or weaken a domain
validation rule.

An incompatible request or response change requires a new major contract
version and a distinct tool/version registration. A minor version may add only
optional fields, additional explicit error codes, or stricter documented output
redaction that preserves the previous bounded payload shape. Removing or
reinterpreting a field, enum value, cursor binding, side effect, approval rule,
or idempotency rule is incompatible. Unknown input fields are rejected.

## 2. Authentication And Trust Boundary

The intended deployment is one long-lived Streamable HTTP MCP service bound
only to `127.0.0.1`, protected by a generated bearer credential stored with
user-only permissions. It validates an applicable `Origin` policy before
dispatch. Authentication, configuration installation, credential storage,
origin policy for non-browser clients, token rotation, loopback binding
behavior, and the supported Windows credential/protection mechanism remain
**deployment and Windows decision points** until accepted `DEP` and Buildout 11
evidence exist.

The transport authenticates access to the MCP adapter. It does not make the
MCP caller an approval principal. A bearer, transport client ID, claimed user,
process ID, hostname, MCP elicitation response, browser origin, or agent
identity MUST NOT issue, sign, import, consume, or mark an execution
authorization. These values may be recorded only as bounded, untrusted audit
metadata according to `AUTH` and `WF`.

The adapter MUST:

1. reject missing, malformed, expired, or unauthorized bearer credentials
   before any application dispatch;
2. reject a request whose Origin violates the accepted local-origin policy;
3. redact credentials, authorization material, cookies, headers, raw vendor
   objects, local paths outside admitted diagnostics, and unbounded model data
   from protocol responses, logs, errors, and audit payloads;
4. route every application operation through the service/orchestrator and its
   serialized owner where a vendor read is needed; and
5. keep MCP client disconnect, cancellation, or timeout from owning or
   terminating the service, lease, or PowerFactory session.

No remote listener, reverse proxy, cross-origin browser access, credential
format, local-principal provider, or Windows firewall/ACL claim is admitted by
this provisional version.

## 3. Common Typed Envelopes

Every tool accepts exactly one generated request payload and returns exactly
one generated success or error envelope. The outer MCP `tools/call` protocol
result is transport framing only; product callers MUST consume the payload
below rather than parse display text.

### 3.1 Request envelope

```json
{
  "contract_version": "mcp-operation-contracts/v0.1.0",
  "request_id": "uuid",
  "correlation_id": "uuid-or-omitted",
  "expected_workflow_version": 7,
  "idempotency_key": "opaque-bounded-key-or-omitted",
  "deadline_ms": 30000,
  "payload": {}
}
```

`request_id` is a client-generated UUID used for tracing only. The service
creates a correlation ID when absent. `expected_workflow_version` is required
for a workflow command and prohibited for operations with no workflow target.
`idempotency_key` is required or prohibited by the operation catalog below; it
is never implicitly derived from a request ID. `deadline_ms` is optional and
bounded by an accepted server configuration. It expresses client wait time,
not a request to interrupt an already-started owner call.

The typed `payload` contains product UUIDs, named schema/policy versions, and
identity/revision values required by its operation. It MUST NOT accept raw
PowerFactory object handles, arbitrary PowerFactory locators, arbitrary Python
expressions, arbitrary filesystem paths, free-form attribute names, transport
credentials, authorization tokens, or an agent-provided approval result.

### 3.2 Success envelope

```json
{
  "contract_version": "mcp-operation-contracts/v0.1.0",
  "request_id": "uuid",
  "correlation_id": "uuid",
  "operation": {
    "operation_id": "uuid-or-omitted",
    "status": "COMPLETED|ACCEPTED|IN_PROGRESS",
    "workflow_id": "uuid-or-omitted",
    "workflow_version": 8,
    "recovery_required": false
  },
  "payload": {},
  "page": {
    "next_cursor": "opaque-cursor-or-null",
    "snapshot_revision": "typed-revision",
    "expires_at": "RFC3339-UTC"
  },
  "warnings": []
}
```

Fields in `operation` and `page` are present only where their generated schema
declares them. A durable operation includes a service-issued `operation_id`
before the adapter acknowledges queued or started work. `payload` contains
only schema-validated, bounded typed records. Warnings are categorized, bounded
evidence references; they cannot turn an unavailable, stale, or unverified
result into a successful engineering assertion.

### 3.3 Error envelope

```json
{
  "contract_version": "mcp-operation-contracts/v0.1.0",
  "request_id": "uuid-or-null",
  "correlation_id": "uuid",
  "error": {
    "code": "ENUMERATED_CODE",
    "category": "AUTHENTICATION|VALIDATION|CONFLICT|NOT_FOUND|LIMIT|UNAVAILABLE|IN_PROGRESS|SAFE_MODE|RECOVERY_REQUIRED|INTERNAL",
    "message": "bounded-redacted-message",
    "retryable": false,
    "operation_id": "uuid-or-null",
    "workflow_id": "uuid-or-null",
    "current_workflow_version": 8,
    "recovery_guidance": "enum-or-bounded-guidance"
  }
}
```

Generated error enumerations include at least `UNAUTHENTICATED`,
`ORIGIN_REJECTED`, `INVALID_REQUEST`, `UNSUPPORTED_CONTRACT_VERSION`,
`WORKFLOW_VERSION_CONFLICT`, `IDEMPOTENCY_CONFLICT`, `CURSOR_INVALID`,
`CURSOR_EXPIRED`, `PAYLOAD_LIMIT_EXCEEDED`, `SAFE_MODE`,
`CONTEXT_MISMATCH`, `LEASE_FENCED`, `AUTHORIZATION_UNAVAILABLE`,
`RECOVERY_REQUIRED`, `OPERATION_IN_PROGRESS`, and `SERVICE_UNAVAILABLE`.
`AUTHORIZATION_UNAVAILABLE` means no independently issued authorization is
available; it MUST NOT disclose an approval secret or be interpreted as an
approval prompt/result. Errors are bounded, serializable, and actionable, but
never contain stack traces, native handles, credentials, or raw engine logs.

## 4. Operation Classes And Admitted Catalog

Tool annotations accurately declare the classes below, but server-side domain
and workflow admission enforce them. An annotation, client claim, or tool name
cannot authorize a side effect.

| Class | Side effect | Idempotency | Admitted tools |
| --- | --- | --- | --- |
| `OBSERVE` | No live context activation or durable workflow mutation; may read an immutable projection/store. | Prohibited. | `get_session_status`, `get_model_context`, `get_project_briefing`, `query_model_graph`, `get_asset_context`, `trace_electrical_path`, `get_impact_zone`, `get_calculation_run`, `find_violations`, `compare_results`, `get_approval_request_status`, `get_operation_status`, `get_workflow_status`, `list_pending_approval_requests`, `get_change_history`, `get_recent_activity` |
| `LIVE_READ` | Bounded serialized-owner read/calculation and immutable result persistence; no model mutation. | Required where a durable operation/run is created. | `open_project_context`, `refresh_model_context`, `run_validated_load_flow` |
| `PREVIEW` | Creates a bounded, immutable workflow preview only; it never changes the engineering model. | Required. | `preview_area_load_scaling`, `preview_rollback` |
| `EXECUTION_REQUEST` | Enters the application admission path for an existing workflow. A subsequent effect is possible only after independent authorization, lease/fencing, revalidation, and `REC` intent. | Required. | `execute_change`, `execute_rollback` |

`OBSERVE` does not mean unrestricted: every operation has an explicit typed
query, project/context scope, authorization check, data-redaction policy, and
payload limit. `LIVE_READ` is not a mutation class even when it creates a
durable calculation or context record. It may be denied for safe mode,
unavailable owner, context conflict, lease rules, or unsupported PowerFactory
evidence without changing model state.

`query_model_graph` accepts only generated discriminated variants:
`neighborhood`, `path`, `area_assets`, `impact_zone`, and `topology_diff`.
There is no arbitrary graph expression language or query execution endpoint.
No generic `set_attribute`, raw PowerFactory invocation, raw object lookup,
filesystem export/import, command runner, mutation workspace selector, or
unregistered capability is an MCP tool.

### Approval non-exposure invariant

The catalog MUST NOT contain `approve`, `reject`, `confirm`, `authorize`,
`issue_authorization`, `consume_authorization`, approval-token import/export,
or any equivalent operation. `get_approval_request_status` and
`list_pending_approval_requests` return only bounded status/summaries admitted
by `AUTH`; they have no decision, nonce, CSRF, credential, or authorization
side effect. `execute_change` and `execute_rollback` are execution requests,
not approvals, and cannot accept a decision/token/boolean to bypass the
independent authority.

## 5. Identity, Pagination, And Payload Bounds

Each request and response carries the exact product UUIDs and, where relevant,
`configuration_key`, `live_state_fingerprint`, `extraction_revision`,
`workspace_revision`, `calculation_input_digest`, snapshot ID, policy version,
workflow ID, and expected/resulting workflow version required by its underlying
contract. `ID` owns their representation and meaning. Names, paths, locators,
timestamps, raw handles, and result filenames are never substitutes.

A cursor is an opaque server-issued typed value. Its canonical signed or
durably looked-up binding contains at least:

1. operation/query variant and contract version;
2. authenticated service scope and permitted project/context scope;
3. immutable extraction revision, result snapshot ID, or activity-event range;
4. normalized filter/sort digest and page size; and
5. issue time and expiry.

A cursor cannot be reused for another query type, filter, page size, contract,
principal/service scope, extraction/result revision, or after expiry. Invalid,
stale, altered, mismatched, or expired cursors fail with a typed cursor error;
the adapter never silently resets pagination or exposes cursor contents.

Every generated tool contract names its request bytes, response bytes, item
count, page size, string length, recursion depth, evidence-reference count,
cursor age, and server wait-time limits from an accepted versioned limits
configuration. No request or response is unbounded. A limit breach returns
`PAYLOAD_LIMIT_EXCEEDED` or a tool-specific typed validation error before a
vendor effect. Exact initial thresholds, storage location, and Windows-specific
path/credential limits are pending deployment evidence and MUST NOT be inferred
from this provisional document.

## 6. Idempotency, Queueing, Cancellation, And Timeout

For every operation requiring an idempotency key, the durable application layer
binds `(principal/service scope, operation name, workflow ID when applicable,
key, canonical request digest, contract version)` to the first operation ID
and result/status. A repeat with the same binding returns the original durable
status/result. Reusing the key with a different request digest, operation,
workflow, principal/service scope, or contract major version fails with
`IDEMPOTENCY_CONFLICT` before a new workflow, lease, authorization consumption,
or vendor effect.

The adapter records a durable operation ID before acknowledging queued work.
Cancellation received before that work enters the serialized-owner queue may
cancel the queued operation according to `WF`. Cancellation or a client
deadline after an owner call has started MUST NOT cancel, replay, or duplicate
the underlying PowerFactory call. The client receives `OPERATION_IN_PROGRESS`
with the durable operation ID and polls `get_operation_status`; it MUST NOT
resubmit under a new idempotency key to obtain completion.

`deadline_ms` may bound adapter wait only. Owner, gateway, lease, workflow,
and reconciliation timeouts remain governed by `DEP`, `LEASE`, `WF`, and `REC`.
A transport timeout is never evidence that a native operation failed or had no
effect. The retained operation record reports only observed state or a
recovery-required classification.

## 7. Compatibility, Registration, And Acceptance Cases

Tool registration is allowlist-based. A service starts in read-only mode unless
the accepted deployment/security configuration and the underlying operation
contracts permit a stronger class. Execution tools are registered only after
their operation, authorization, lease, workflow, recovery, policy, and
PowerFactory evidence are accepted. A missing prerequisite omits the tool or
returns `SAFE_MODE`; it never exposes a reduced-safety alternative.

Portable and protocol tests must at minimum establish:

| Case | Exercise | Required result |
| --- | --- | --- |
| `MCP-001` | Generate schemas twice from fixed domain contracts | Identical inventory/order/digest; no handwritten public shape. |
| `MCP-002` | Enumerate tools | Exact allowlist, correct class annotations, and no approval/generic-write/raw-vendor tool. |
| `MCP-003` | Missing/invalid bearer or invalid Origin | Rejected before application dispatch; no operation/audit secret exposure. |
| `MCP-004` | Unknown fields, wrong version, malformed UUID/revision, oversize payload | Typed validation/limit error before queue or vendor work. |
| `MCP-005` | Reuse/tamper/expire cursor or alter query/revision/page size | Typed cursor error; no silent restart or data-scope expansion. |
| `MCP-006` | Repeat a preview/execution request before and after restart | Original durable operation only; mismatched key/digest conflicts; no duplicate effect. |
| `MCP-007` | Client cancellation/deadline before and after queue/owner start | Pre-queue cancellation obeys `WF`; started work persists and is pollable by operation ID. |
| `MCP-008` | Codex-compatible and second MCP client invoke identical read workflow | Equivalent typed application state; neither imports PowerFactory or changes domain behavior. |
| `MCP-009` | Client attempts approval issuance/decision/token import or passes approval-shaped fields to execute | No callable route/schema field; rejected before authorization/workflow mutation. |
| `MCP-010` | Read-only and safe-mode registration checks | Only admitted read tools are reachable; unavailable execution stays disabled. |

Acceptance additionally requires Buildout 11's exact dependency-matrix
commands, authenticated localhost/origin evidence, client transcripts,
timeout/idempotency traces, import/layer scans, generated schema artifacts,
and an exact-commit Windows record for the final end-to-end environment. Until
then this contract is preparation evidence only and MUST NOT be used to claim
that any Streamable HTTP transport, bearer protection, origin policy, client
configuration, or PowerFactory execution path has been proven.

## References

- Product roadmap: Buildout 11 and its MCP implementation rules.
- `deployment-and-process-ownership/v0.1.0` (`DEP`).
- `identity-and-revisions/v0.1.0` (`ID`).
- `approval-authority/v0.1.0` (`AUTH`).
- `context-lease-and-fencing/v0.1.0` (`LEASE`).
- `workflow-state-machine/v0.1.0` (`WF`).
- `crash-reconciliation/v0.1.0` (`REC`).
- `load-flow-and-violation-policy/v1` (`LF`).
- Open-source adoption ledger decisions `PM-02`, `PM-03`, `PM-R03`, and
  `PM-R04`.
