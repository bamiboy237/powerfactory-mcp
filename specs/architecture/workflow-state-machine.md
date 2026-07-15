# Workflow State Machine Contract

| Field | Value |
| --- | --- |
| Specification key | `WF` |
| Specification ID | `workflow-state-machine/v0.1.0` |
| Status | **PROVISIONAL - Buildout 6 preparation; Windows evidence pending** |
| Governing buildout | Buildout 6 - Durable Workflow, Authorization, and Audit Foundation |
| Acceptance prerequisite | Accepted DEP, ID, LEASE, REC, approval-authority evidence, and exact-commit workflow/recovery evidence |

## 1. Purpose And Boundaries

This specification defines the durable orchestration state for one engineering
workflow across client disconnects, process restarts, authorization waits, and
reconciliation. It applies to one high-level proposed operation and its
compensating rollback branch. It is authoritative for legal workflow
transitions, command compare-and-swap, idempotency, and the ordering of durable
workflow/audit records.

It does not define authorization issuance, lease ownership/fencing, vendor
calls, calculation result semantics, or reconciliation classifications. Those
belong respectively to the approval-authority, `LEASE`, gateway/operation,
calculation policy, and `REC` specifications. This workflow coordinates those
subsystems and fails closed when any of them cannot prove its precondition.

Workflow state is deliberately distinct from operation state, lease state,
calculation state, rollback workspace state, and reconciliation classification.
For example, an `IN_FLIGHT` operation does not make a workflow successful, and
`AFTER_OBSERVED` is a `REC` finding that requires a workflow transition rather
than an implicit continuation.

## 2. Durable Workflow Record

Every workflow row contains a UUID `workflow_id`, `workflow_version`, current
state, operation specification/version, authenticated request principal,
configuration key, proposal digest, dependency-scoped live-state fingerprint,
extraction and workspace revisions where relevant, current lease/fencing
reference, authorization reference/consumption status, last operation ID,
correlation ID, recovery disposition, timestamps, and sanitized evidence
references.

`workflow_version` follows `ID`: it increments once in the same transaction as
every workflow state or binding/recovery change. Every workflow command carries
an expected workflow version and caller-supplied idempotency key. The durable
command record binds its canonical request digest, original operation ID,
result/status, and resulting workflow version.

The database transaction for a consequential command writes, in order:

1. an audit `requested` event and idempotency command record;
2. workflow transition intent and all required bindings;
3. the changed workflow/version row; and
4. an audit event describing the durable transition.

Only after that commit may a lease or serialized-owner operation be invoked.
The resulting observation is appended later; no audit event calls an external
effect successful before the required live verification is durable.

## 3. Workflow States

```text
NEW
PREVIEWING
AWAITING_AUTHORIZATION
EXECUTION_ADMISSION
EXECUTING
CALCULATING
VERIFYING
COMPLETED
FAILED_BEFORE_EFFECT
RECONCILIATION_REQUIRED
QUARANTINED
ROLLBACK_PREVIEWING
AWAITING_ROLLBACK_AUTHORIZATION
ROLLBACK_ADMISSION
ROLLING_BACK
ROLLBACK_VERIFYING
ROLLED_BACK
ABANDONED
```

`NEW` has no durable preview. `PREVIEWING` is an admitted read-only live
envelope under a preview lease. `AWAITING_AUTHORIZATION` is a durable
suspension: it holds a complete immutable preview but no context lease,
fencing token, native call, or execution authorization consumption.

`EXECUTION_ADMISSION` and `ROLLBACK_ADMISSION` are short durable gates that
reacquire a new lease token and revalidate all bindings before a write. An
authorization is consumed only in the same transaction that enters the
corresponding admitted execution state. `EXECUTING`, `CALCULATING`,
`VERIFYING`, `ROLLING_BACK`, and `ROLLBACK_VERIFYING` each reference separate
durable operation/calculation records; they do not imply their outcomes.

`RECONCILIATION_REQUIRED` and `QUARANTINED` are non-admissible for new live
effects. `COMPLETED`, `FAILED_BEFORE_EFFECT`, `ROLLED_BACK`, and `ABANDONED`
are terminal for their branch. A compensating rollback is not a reverse
transition; it is a separately previewed, independently authorized branch with
its own command/idempotency/lease/reconciliation records.

## 4. Executable Transition Table

All commands require the expected `workflow_version` and an idempotency key.
`PF effect` means a serialized owner operation only after durable preparation.

| Command | Legal source state | Guards | Durable preparation | PF effect | Reconciliation/retry | Destination |
| --- | --- | --- | --- | --- | --- | --- |
| `start_preview` | `NEW` | operation/project allowlist; verified context admission; no safe-mode/quarantine | bind request digest/spec, create operation, audit | acquire `LEASE` preview and bounded live reads | failed before any effect may retry only with same command record; unknown owner result goes to recovery | `PREVIEWING` |
| `record_preview` | `PREVIEWING` | complete typed preview; configuration/fingerprint freshness per operation policy | persist immutable preview/digest, release preview lease, audit | none after release | lost release/owner state is recovery-required | `AWAITING_AUTHORIZATION` |
| `reject_or_expire_preview` | `AWAITING_AUTHORIZATION` | authorization absent, rejected, or expired | record reason/audit; no authorization consumption | none | repeat returns original result | `ABANDONED` |
| `admit_execution` | `AWAITING_AUTHORIZATION` | independently issued, unconsumed authorization binds exact preview digest, principal, operation, configuration, fingerprint, workspace, strategy, expiry, execution ID, and expected version | record admission intent; acquire new `LEASE` execution token; re-read/revalidate every binding; atomically consume authorization only if all pass | bounded live revalidation only | any mismatch/expiry/reused authorization rejects without write; unavailable evidence invokes `REC` | `EXECUTION_ADMISSION` then `EXECUTING` |
| `apply_change` | `EXECUTING` | current lease/token, workflow CAS, write allowlist, exact target preconditions | commit per-target `REC` intent before each submission; audit submitted | authorized mutation through serialized owner | known no-effect failure -> `FAILED_BEFORE_EFFECT`; uncertain/partial outcome -> `RECONCILIATION_REQUIRED` | `CALCULATING` only after all required observed writes; otherwise recovery state |
| `run_calculation` | `CALCULATING` | post-write context/lease and policy inputs remain valid | persist calculation command/input digest/operation | serialized calculation | timeout/unknown/native failure follows operation/REC rules; no false convergence | `VERIFYING` on durable result |
| `verify_execution` | `VERIFYING` | required postconditions/result policy completed | persist observations, comparison, validation evidence, release lease, audit | bounded verification reads only | failed or unavailable verification never claims success; use recovery/quarantine as required | `COMPLETED`, `RECONCILIATION_REQUIRED`, or `QUARANTINED` |
| `start_rollback_preview` | `COMPLETED` or permitted recovery/manual disposition | compensating operation is admitted and original evidence is available | create separate rollback branch/request; audit | acquire preview lease and read live values | unavailable/changed values block rollback execution | `ROLLBACK_PREVIEWING` |
| `record_rollback_preview` | `ROLLBACK_PREVIEWING` | complete compensating preview | persist preview, release lease, audit | none after release | recovery if lease release uncertain | `AWAITING_ROLLBACK_AUTHORIZATION` |
| `admit_rollback` | `AWAITING_ROLLBACK_AUTHORIZATION` | separate valid rollback authorization and exact compensation bindings | new lease token, full live revalidation, atomic authorization consumption | bounded live revalidation | mismatch blocks before write | `ROLLBACK_ADMISSION` then `ROLLING_BACK` |
| `apply_rollback` | `ROLLING_BACK` | current token and compensating preconditions | per-target `REC` intent and audit | serialized compensating write | uncertain result -> recovery; no automatic repeat | `ROLLBACK_VERIFYING` |
| `verify_rollback` | `ROLLBACK_VERIFYING` | required restoration/validation completed | persist evidence and release lease | bounded verification reads only | unavailable/diverged state quarantines | `ROLLED_BACK`, `RECONCILIATION_REQUIRED`, or `QUARANTINED` |
| `reconcile` | any recovery-required state | `DEP` establishes non-concurrency; `REC` fresh observation admissible; lease token valid | append recovery event/classification and update bindings/version | only bounded fresh reads through owner | `BEFORE`, `AFTER_OBSERVED`, `DIVERGED`, `UNAVAILABLE` follow `REC`; never replay a started write | state selected explicitly by recovery policy, otherwise `QUARANTINED` |
| `operator_disposition` | `RECONCILIATION_REQUIRED` or `QUARANTINED` | authenticated independent local principal; allowed `REC` disposition | append-only disposition/audit | none, except a separately authorized compensation workflow | no bypass of authorization, fencing, or recovery evidence | `ABANDONED`, rollback preview state, or remains quarantined |

An implementation may split a listed transition into internal durable phases
only when each phase has the same or stricter guards, audit ordering, recovery
behavior, and idempotency binding. It must not add an implicit path to a live
write, authorization consumption, or success state.

## 5. Idempotency And Compare-And-Swap

A command first looks up its idempotency key within the workflow. Same command
name and canonical request digest return the original operation status/result,
including across restart. A different command or digest with that key is an
idempotency conflict. For a new key, the transition transaction compares the
stored `workflow_version` against the caller's expected version; a mismatch
returns `WORKFLOW_VERSION_CONFLICT` and has no lease, authorization, or vendor
effect.

The workflow version is rechecked whenever a later transition consumes an
authorization, records an operation observation that changes admission, starts
reconciliation, or changes quarantine/disposition. A client timeout returns the
persisted operation status; it neither cancels a started operation nor permits a
new key to replay its effect.

## 6. Authorization Suspension And Lease Handling

The only normal waiting state is `AWAITING_AUTHORIZATION` or
`AWAITING_ROLLBACK_AUTHORIZATION`. Before either state is published, the preview
and all preview evidence are persisted and the preview lease is released. A
restart in either waiting state must restore the workflow without contacting
PowerFactory or retaining a live lease.

After authorization, admission always obtains a new `LEASE` fencing token and
rereads configuration key, live-state fingerprint, workspace revision, target
values, proposal digest, principal, operation/strategy, authorization expiry,
and workflow version. Any difference invalidates the authorization or proposal
according to `ID`; it cannot be repaired by retaining the old lease/token.

## 7. Recovery Handoffs And Audit

`DEP` decides singleton/engine recovery admission. `LEASE` decides whether any
old holder/token is stale and whether a new live envelope can exist. `REC`
classifies uncertain started work after fresh bounded observation. This workflow
state machine receives those facts through durable references and selects only
the transitions allowed above.

On restart, a workflow in preview/execution/calculation/verification/rollback
with an uncertain owner operation transitions to `RECONCILIATION_REQUIRED` in a
durable transaction before any live recovery call. A workflow in an
authorization-wait state remains suspended with no lease. A workflow with a
`DIVERGED` or `UNAVAILABLE` REC classification transitions to `QUARANTINED` and
requires independent operator disposition; it must not continue calculation,
verification, or rollback automatically.

Audit events are append-only and causally ordered. At minimum, history must
show `requested`, `preview_started`, `preview_persisted`, `lease_released`,
`authorization_requested`, `authorization_observed`, `admission_revalidated`,
`intent_committed`, `submitted`, `owner_returned`, `live_observed`,
`verified`, `recovery_started`, `classified`, `quarantined`, and
`manual_disposition` where applicable. Events cite workflow/operation IDs,
version, request/intent digest, authorization and fencing references, actor
class, timestamp, and sanitized evidence reference. They must not contain raw
vendor handles, credentials, or unrestricted model data.

## 8. Executable Acceptance Cases

| ID | Test | Expected result | Environment |
| --- | --- | --- | --- |
| `WF-001` | Start preview then restart before preview completion | Recover legal durable state or reconciliation; no duplicate read/write envelope | Fake/recovery |
| `WF-002` | Persist preview, wait for authorization, restart | Exact preview/workflow restores; no held lease or PowerFactory call | Fake/recovery |
| `WF-003` | Revalidate changed configuration, fingerprint, workspace, target, or workflow version after authorization | Admission rejects before authorization consumption/write | Fake and Windows |
| `WF-004` | Replay each command idempotency key before/after restart | Original operation/status only; no version increment or duplicate effect | Fake/recovery |
| `WF-005` | Submit stale expected workflow version at every consequential transition | CAS rejection before lease/vendor/authorization side effect | Fake/unit |
| `WF-006` | Crash after intent before or after owner submission | Preserve evidence; enter REC handoff; never replay started write | Fake/recovery and Windows |
| `WF-007` | Expire/revoke authorization during admission | No consume/write; workflow remains wait/abandoned per recorded reason | Fake/contract |
| `WF-008` | Lease expiry or stale fencing token during execution | No new effect from stale owner; uncertain started call enters reconciliation/quarantine | Fake/recovery and Windows |
| `WF-009` | Calculation failure/non-convergence after completed mutation | No `COMPLETED` claim; retain immutable evidence and follow declared recovery/rollback policy | Fake and Windows |
| `WF-010` | REC reports `DIVERGED` or `UNAVAILABLE` | Workflow quarantines; new command/idempotency key cannot bypass it | Fake/recovery |
| `WF-011` | Attempt agent/MCP-created or forged authorization | Admission rejects before lease/write; audit records rejection | Security/contract |
| `WF-012` | Authorized compensating rollback | Separate preview/authorization/token/idempotency lineage; original workflow is not rewritten | Fake/recovery and Windows |
| `WF-013` | Reconstruct audit history from SQLite after client/service restart | Requested, authorized, attempted, observed, reconciled, and compensated sequence is traceable | Fake/integration |

## 9. Acceptance Gate And Open Evidence

This specification remains provisional until the Buildout 6 dependency-matrix
gate passes against an exact commit SHA. Required Windows evidence includes
serialized PowerFactory context behavior, mutation/calculation/verification
interruption, native call timeout/late outcome, context restoration, and
non-concurrent recovery. Evidence must identify release/service pack, licence
capability, extension path, CPython ABI/architecture, fixture identifier,
commands, exit codes, sanitized logs, and observed deviations.

Until the approval-authority, `DEP`, `ID`, `LEASE`, `REC`, and this workflow
contract are accepted, this document authorizes no live workflow or mutation
behavior.

## 10. Traceability

- Product roadmap: Buildout 6 items 1-15, safety/lease/approval policy, and
  primary end-to-end sequence.
- `DEP`: singleton/serialized owner, timeout/quarantine, and restart admission.
- `ID`: workflow CAS/idempotency and all context-bound invalidation rules.
- `LEASE`: exclusive envelope, release while awaiting authorization, fencing,
  expiry, and stale-owner handling.
- `REC`: write-ahead intent, observation classification, quarantine, and
  compensating recovery.
