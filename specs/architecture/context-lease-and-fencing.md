# Context Lease And Fencing Contract

**Specification key:** `LEASE`

**Version:** `0.1.0`

**Status:** PROVISIONAL - Buildout 6 preparation; no acceptance claim is made.

## 1. Purpose And Authority

This contract prevents interleaving multi-step workflows against the single
PowerFactory session. It defines a durable workflow-level context lease and
monotonically increasing fencing token. It supplements, and never replaces,
the singleton service owner and serialized vendor-call owner defined by
`deployment-and-process-ownership/v1` (`DEP`).

This contract is authoritative for lease admission, expiry, stale-owner
rejection, and recovery. `identity-and-revisions/v1` (`ID`) remains
authoritative for `configuration_key`, `live_state_fingerprint`,
`workspace_revision`, and `workflow_version`. Approval issuance and workflow
transitions are defined by their companion specifications.

## 2. Scope And Non-Goals

A lease scope is one verified `configuration_key` within one authenticated
singleton service scope. At most one workflow holds a live-context lease in
that scope. A lease admits a workflow envelope, not an arbitrary client,
gateway call, or raw PowerFactory object.

Immutable SQLite snapshots, immutable calculation results, operation-status
polling, and bounded graph reads may proceed without a lease. A request that
activates, changes, or performs a live read whose context could disturb a
leased envelope requires the holder's current fencing token. Individual vendor
call serialization does not grant a lease and cannot substitute for one.

This contract does not authorize mutation, define PowerFactory context
activation semantics, create an approval, or infer a native call outcome.

## 3. Durable Records

The authoritative store contains one current lease row per lease scope and an
append-only lease-event stream. A current lease row contains at least:

| Field | Requirement |
|---|---|
| `lease_id` | Product UUID for one lease incarnation. |
| `scope_key` | Canonical service-scope digest plus exact `configuration_key`. |
| `workflow_id` | UUID of the sole admitted workflow. |
| `workflow_version` | Version expected when the lease was acquired or renewed. |
| `fencing_token` | Positive integer, strictly greater than every prior token for the same scope. |
| `mode` | `PREVIEW`, `EXECUTION`, or `ROLLBACK`. |
| `state` | One of the states in section 4. |
| `issued_at`, `expires_at` | UTC timestamps with a positive bounded duration. |
| `owner_instance_id` | Authenticated singleton instance identity, never a PID alone. |
| `operation_id` | Present only while an admitted atomic vendor call is in flight. |
| `configuration_key` | Exact verified context at admission. |
| `recovery_disposition` | Empty unless expiry, crash, or reconciliation requires it. |

Lease events record correlation ID, actor/command, prior and new state,
workflow/version, fencing token, reason, timestamp, and durable evidence
references. Events do not record raw vendor objects, credentials, unrestricted
model data, or misleading success claims.

Rows are written with a transaction and compare-and-swap predicates on both the
current fencing token and workflow version. A token is never reused, including
after release, expiry, service restart, database restoration, or lease-row
cleanup.

## 4. Lease State Machine

```text
AVAILABLE
  -> HELD_PREVIEW
  -> HELD_EXECUTION
  -> HELD_ROLLBACK
  -> EXPIRED
  -> IN_FLIGHT_EXPIRED
  -> RECONCILIATION_REQUIRED
  -> QUARANTINED
```

`AVAILABLE` has no current holder. `HELD_PREVIEW`, `HELD_EXECUTION`, and
`HELD_ROLLBACK` have one live holder. `EXPIRED` rejects the prior holder and
may admit a later workflow only after all required release/recovery checks.
`IN_FLIGHT_EXPIRED` means a lease expired after a native call was durably marked
started; it is not permission to alter or restore live context. `RECONCILIATION_REQUIRED`
and `QUARANTINED` admit no live operation.

| Command | Legal source | Guards and durable action | Destination |
|---|---|---|---|
| `acquire_preview` | `AVAILABLE` | CAS workflow version; verify configuration; mint next token; append intent/event before live read | `HELD_PREVIEW` |
| `release_for_authorization` | `HELD_PREVIEW` | No vendor call in flight; persist preview and `AWAITING_AUTHORIZATION`; clear current holder | `AVAILABLE` |
| `acquire_execution` | `AVAILABLE` | Authorization is independently valid and unconsumed; mint a new token; revalidate exact configuration, live fingerprint, workspace revision, target values, proposal digest, principal, operation, and workflow version | `HELD_EXECUTION` |
| `acquire_rollback` | `AVAILABLE` | Valid rollback authorization and compensating-operation preconditions; mint a new token and revalidate | `HELD_ROLLBACK` |
| `start_atomic_call` | held state | Holder token and workflow/version CAS match; operation intent is durable first | same held state with `operation_id` |
| `finish_atomic_call` | held state | Match lease ID, token, and operation ID; persist observed outcome before release | held state without `operation_id` or recovery state |
| `release` | held state | No atomic call in flight; required postcondition is durably known | `AVAILABLE` |
| `expire` | held state | Clock passes `expires_at` and no atomic call is in flight | `EXPIRED` |
| `expire_in_flight` | held state | Clock passes `expires_at` with durable started operation | `IN_FLIGHT_EXPIRED` |
| `recover` | expiry/recovery state | Original outcome, engine identity, context disposition, and workflow reconciliation are durable and admissible | `AVAILABLE` or `QUARANTINED` |

Every command has an idempotency key. Repeating a completed or in-flight command
returns its original operation state/result. A mismatched expected
`workflow_version`, holder, token, or lease state is rejected before a vendor
effect.

## 5. Fencing And Exclusive Envelope

The lease manager mints one strictly monotonic fencing token per scope. Every
live context operation carries `(lease_id, fencing_token, workflow_id,
workflow_version, configuration_key)`. The serialized owner validates those
values immediately before dispatching a vendor call and before accepting its
postcondition. A token lower than the current scope token, a different lease
ID, expired holder, or mismatched workflow/configuration is `STALE_FENCE` and
is rejected without a new live effect.

An execution or rollback envelope begins only after `acquire_execution` or
`acquire_rollback` completes. It covers precondition reads, write-ahead intent,
authorized mutation, calculation, verification, and durable observation. No
other workflow may activate a context or issue disruptive live reads until the
envelope releases or is quarantined. The authorization wait is explicitly
outside the envelope: preview release removes the holder before a human may
consider the proposal, and execution reacquisition always uses a newer token.

## 6. Expiry, Stale Owners, And Recovery

Expiry prevents new work by the old holder. It never proves a native call was
cancelled. If expiry occurs before `start_atomic_call`, the operation is
rejected and no vendor call begins. If it occurs after durable start, the state
is `IN_FLIGHT_EXPIRED`; the owner must retain the live context until the call
outcome is known, or the engine is quarantined and workflow reconciliation is
complete.

On service or worker restart, all held leases are loaded before any new engine
acquisition. A held lease with no durable in-flight operation becomes expired
and its prior token is stale. A lease with an in-flight, client-timed-out,
engine-unresponsive, or uncertain operation becomes
`RECONCILIATION_REQUIRED`. No replacement lease or engine session is admitted
until `DEP` recovery rules establish non-concurrency and the workflow record is
classified. Unknown mutation, context-restoration, engine identity, or
database-commit state enters `QUARANTINED` or safe mode; it is never guessed
away.

An authenticated operator recovery action is required to clear quarantine.
Recovery appends an audit event and preserves prior lease, intent, and
observation evidence. It does not create a new authorization or silently renew
an expired lease.

## 7. Audit Requirements

Before and after acquire, renew, release, expiry, stale-fence rejection,
atomic-call start/finish, recovery admission, quarantine, and operator recovery,
append an audit event. An event may say `attempted`, `started`, `observed`, or
`reconciled`; it may say `succeeded` only after the relevant live postcondition
is verified. Audit history must reconstruct which workflow held which token,
when it released for authorization, which token admitted execution, and why a
lease was rejected, expired, or quarantined.

## 8. Executable Acceptance Cases

Acceptance requires automated tests tied to the exact tested commit. Cases
marked Windows require the supported engine environment and sanitized evidence.

| ID | Test | Expected result | Environment |
|---|---|---|---|
| `LEASE-001` | Two workflows acquire the same verified scope concurrently | Exactly one `HELD_*` lease; one minted token; loser receives structured busy result | Fake/unit |
| `LEASE-002` | Start preview, persist proposal, enter authorization wait, restart | Workflow remains `AWAITING_AUTHORIZATION`; no lease row remains held | Fake/recovery |
| `LEASE-003` | Reacquire after authorization | New token is greater than preview token; all `ID` preconditions are reread and must match | Fake/integration |
| `LEASE-004` | Submit stale token after a later acquire | Structured `STALE_FENCE`; no vendor call or workflow version increment | Fake/contract |
| `LEASE-005` | Expire before atomic call start | Stale owner rejected; no call started; later acquisition gets a newer token | Fake/unit |
| `LEASE-006` | Expire during a fault-injected atomic call | No restore/release while outcome unknown; only reconciliation or quarantine follows | Fake/recovery and Windows |
| `LEASE-007` | Restart with held lease and durable in-flight operation | Lease/workflow becomes reconciliation-required; no second session or live admission | Fake/recovery and Windows |
| `LEASE-008` | Attempt different configuration or disruptive read during execution envelope | Rejected before live effect; immutable cached reads remain available | Fake/integration and Windows |
| `LEASE-009` | Replay acquire/release/execute command idempotency key | Return original status; no second token, call, or side effect | Fake/recovery |
| `LEASE-010` | Crash after intent and before observed write postcondition | Preserve token, intent, audit chain, and reconciliation classification | Fake/recovery and Windows |
| `LEASE-011` | Recover quarantined owner without authenticated operator action | Recovery rejected; no new lease or engine session | Fake/security |
| `LEASE-012` | PowerFactory context activation and stale-token call race | At most one admitted context; original/native outcome and non-concurrency are evidenced | Windows |

## 9. Unresolved Windows Evidence And Acceptance Gate

This specification cannot be accepted until Windows validation, against an
exact commit SHA, demonstrates PowerFactory 2026 behavior for serialized
context activation, blocked native calls, expiry during a call, post-call
context restoration, process/engine identity, and no-concurrent-session
recovery. Evidence must include PowerFactory release/service pack, licence
capability, `powerfactory.pyd` path, CPython ABI/architecture, fixture
identifier, commands, exit codes, sanitized logs, and engineering observations.

Acceptance additionally requires accepted `DEP`, `ID`, approval-authority,
workflow-state-machine, and crash-reconciliation specifications plus the
Buildout 6 dependency-matrix gate. Until then, no implementation may expose a
live execution path on the basis of this provisional contract.

## 10. Traceability

- Product roadmap: Buildout 6 items 1-14, section 4.1 safety/lease policy,
  ADR 10, and the primary end-to-end sequence.
- `DEP`: singleton ownership, serialized native calls, timeout/quarantine, and
  crash/stale-owner recovery.
- `ID`: configuration/fingerprint/workspace/workflow identities, invalidation,
  compare-and-swap, and idempotency.
