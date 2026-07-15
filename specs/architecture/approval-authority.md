# Approval Authority Specification

| Field | Value |
| --- | --- |
| Specification key | `AUTH` |
| Specification ID | `approval-authority/v0.1.0` |
| Status | **PROVISIONAL - local-principal and Windows evidence pending** |
| Governing buildout | Buildout 6 - Durable Workflow, Authorization, and Audit Foundation |
| Acceptance prerequisite | Accepted DEP, ID, LEASE, REC, workflow-state-machine, and exact-commit authority evidence |

This specification defines the independent local authority that may issue one execution authorization for one previously persisted proposal. It supplements deployment ownership (`DEP`), identity/revisions (`ID`), context lease/fencing (`LEASE`), and crash reconciliation (`REC`). It does not define proposal computation, workflow transitions, native mutation semantics, or an agent/MCP approval tool. Portable fake authority work is allowed; no live execution path is authorized by this provisional document.

## 1. Authority Boundary

The authority is a local service surface separate from the MCP/agent request surface. An MCP client, agent, gateway, workflow worker, environment variable, command-line argument, or arbitrary local process MUST NOT issue, mint, sign, import, or mark consumed an execution authorization. The workflow service may create only an `ApprovalRequest` for an immutable preview and may query an authorization's bounded status.

The authority is reachable only through an authenticated loopback interaction owned by the current OS user. Loopback reachability, a process PID, a claimed username, agent identity, client identity, browser-provided display name, or a bearer supplied by an MCP client is not authenticated-principal evidence. The concrete local principal provider and its protected credential store are **deployment and Windows decision points**; a deterministic fake provider may be used for portable tests.

An authority decision is made against a server-rendered, bounded, immutable proposal summary. It includes exact target count, requested operation, before/proposed values and units, exclusions/warnings, proposal digest, configuration key digest, live-state-fingerprint digest, workspace revision, expected workflow version, policy versions, expiry, and a clear reject/approve choice. It must not display secrets, raw vendor objects, or unrestricted model data.

## 2. Approval Request

Only a preview that is durably persisted, not expired, and in `AWAITING_AUTHORIZATION` may create an `ApprovalRequest`. The request is append-only and binds:

- request UUID, workflow UUID, preview UUID, proposal content digest, request time, and expiry;
- configuration key, live-state fingerprint, workspace revision, operation type, mutation strategy, expected workflow version, and policy/specification versions;
- agent/client identities as untrusted audit metadata only; and
- a one-time authority presentation nonce and a bounded summary digest.

The authority rejects a request whose preview, digest, configuration, fingerprint, workspace revision, workflow version, or expiry no longer matches the durable workflow record. It never reconstructs a proposal from mutable UI input, names, locators, or cached values. Approval requests cannot be edited, renewed, or reused; a new preview produces a new request.

## 3. Authenticated Decision and Web Defenses

The authority creates a short-lived authenticated authority session only after the local principal provider succeeds. The session is bound to one authority instance, current OS user, browser session, approval-request UUID, and a cryptographically random nonce. Credentials, session tokens, and nonces must be protected under DEP's user-only storage and redaction rules.

Approval uses a POST-only confirmation endpoint. The authority MUST validate the authenticated session, one-time request nonce, per-session CSRF token, same-origin `Origin` when present, and `Referer` fallback policy when Origin is absent. It MUST deny cross-origin CORS, reject GET/state-changing navigations, use an HttpOnly/SameSite session cookie, and rotate/invalidate the CSRF token after any terminal decision. A missing, malformed, expired, reused, cross-session, or cross-request nonce/token is a rejection with no authorization side effect.

The authority records one of `APPROVED`, `REJECTED`, `EXPIRED`, `INVALIDATED`, or `CANCELLED` as an append-only decision. A decision is idempotent only for the exact authority session, request nonce, and decision digest; a replay from any other session/request is rejected. Rejection, expiry, cancellation, or invalidation cannot later be changed to approval.

## 4. Execution Authorization

Only an approved request may issue an `ExecutionAuthorization`. It has a fresh UUIDv4 `execution_id`, authority decision ID, authenticated-principal reference, issue/expiry times, and the exact immutable bindings below:

| Binding | Required value |
| --- | --- |
| Proposal | proposal content digest and preview UUID |
| Workflow | workflow UUID and expected `workflow_version` |
| Context | exact `configuration_key` and `live_state_fingerprint` |
| Workspace | workspace UUID and exact `workspace_revision` |
| Operation | operation type, mutation strategy, target set through proposal digest, and specification/policy versions |
| Principal | authenticated principal reference and authority instance ID |
| Execution | unique single-use `execution_id`, decision ID, issue time, and expiry |

Agent/client identity stays audit metadata and cannot be a binding that substitutes for the authenticated principal. Authorization scope is exactly one execution, not a time window, operation family, project, or workflow branch. It cannot be delegated or copied to another service instance.

## 5. Admission, Expiry, and Invalidation

Execution admission occurs only after `LEASE.acquire_execution` obtains a new fencing token and rereads every `ID` precondition. In one durable compare-and-swap transaction, the admission gate verifies authorization state `ISSUED`, authority/request integrity, unexpired time, proposal digest, workflow/version, configuration, fingerprint, workspace revision, operation, strategy, policy, principal binding, and required workflow state; then records the authorization as `CONSUMING` and creates the execution operation record. The write-ahead `REC` intent is committed before any native write.

Authorization states are `ISSUED`, `CONSUMING`, `CONSUMED`, `EXPIRED`, `INVALIDATED`, `REJECTED`, and `CANCELLED`. A second admission attempt for `CONSUMING` or `CONSUMED` returns the original operation status and never starts another call. If admission fails before native submission, the authorization remains consumed for that execution ID; a new attempt requires a new authority decision. After native submission, terminal success, failure, timeout, or reconciliation leaves the authorization `CONSUMED`; it is never returned to `ISSUED`.

The authority or admission gate invalidates an unconsumed authorization on expiry, request cancellation/rejection, authority integrity failure, workflow-version change, preview expiry, changed proposal digest, changed configuration key, changed live-state fingerprint, changed workspace revision, changed operation/strategy/policy, lease admission failure that changes workflow state, or workflow quarantine. Expiry during a started call does not cancel it; the authorization remains consumed and REC/LEASE govern outcome recovery.

## 6. Audit and Privacy

Append audit evidence for request creation, presentation, authentication success/failure category, CSRF/replay rejection, decision, issuance, invalidation, admission attempt, consumption, execution completion, and reconciliation linkage. Records include workflow/request/decision/execution/correlation IDs, principal reference, request/proposal digest, prior/new authorization state, reason category, timestamps, and bounded evidence references.

Audit events never store credentials, session/CSRF/nonces, raw authorization tokens, browser headers, secrets, raw vendor objects, or unbounded proposal/result content. An event may report `approved` after the authority decision, but it must not report native execution success until REC/LEASE evidence proves the live postcondition.

## 7. Executable Acceptance Cases

Portable cases use fixed UUIDs/timestamps, a fake principal provider, a deterministic clock, temporary SQLite, and a fake workflow/lease service. They prove authority behavior only.

| Case | Exercise | Expected result |
| --- | --- | --- |
| `AUTH-001` | Agent/MCP endpoint attempts issuance or state mutation | no authority route/capability; request rejected before authorization creation |
| `AUTH-002` | Valid principal approves immutable request | one `ISSUED` authorization with every required binding |
| `AUTH-003` | Missing/invalid/replayed CSRF token, nonce, Origin, or session | rejection audit; no decision or authorization |
| `AUTH-004` | Reuse approved request from another session or principal | replay rejected; no second authorization |
| `AUTH-005` | Change digest, configuration, fingerprint, workspace revision, workflow version, policy, operation, or strategy after approval | authorization invalidated; execution admission fails before vendor work |
| `AUTH-006` | Expire request before decision or authorization before admission | `EXPIRED`; no issuance/admission |
| `AUTH-007` | Submit same execution ID twice, including after restart | original operation status only; one consumption and no duplicate effect |
| `AUTH-008` | Crash after consume but before/after native submission | no reissue; recovery links original authorization to REC evidence |
| `AUTH-009` | Expire during a started native call | call is not cancelled; authorization remains consumed; recovery proceeds through REC/LEASE |
| `AUTH-010` | Reject/cancel request, then attempt approval | terminal state remains terminal; no authorization |
| `AUTH-011` | Inspect audit/public serializers with credentials, tokens, browser headers, and vendor-shaped values | sensitive values are rejected/redacted and output remains bounded |
| `AUTH-012` | Restart authority with an issued authorization and changed service instance | integrity checks invalidate or preserve only under accepted protected-state rules; no cross-instance delegation |

## 8. Pending Acceptance Evidence

Acceptance requires an exact-commit local principal-provider and protected credential/session-store decision, authenticated loopback deployment evidence, browser CSRF/replay testing, restart/clock behavior, and compatibility with DEP's singleton/recovery model. Windows evidence must record the supported OS authentication mechanism and verify it cannot be forged by an ordinary local process or MCP client. DEP, ID, LEASE, REC, workflow-state-machine, and dependency-matrix gates must also pass. Until then this specification remains provisional and cannot enable live mutation authorization.
