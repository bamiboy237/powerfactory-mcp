# Crash Reconciliation Specification

| Field | Value |
| --- | --- |
| Specification key | `REC` |
| Specification ID | `crash-reconciliation/v0.1.0` |
| Status | **PROVISIONAL - Windows evidence pending** |
| Governing buildout | Buildout 6 - Durable Workflow, Authorization, and Audit Foundation |
| Acceptance prerequisite | Accepted DEP and ID evidence, accepted workflow/lease/approval specifications, and exact-commit Windows recovery evidence |

This specification defines durable evidence and fail-closed recovery for an operation whose process, service, owner, client, or persistence path fails. It complements deployment ownership (`DEP`) and identity/revisions (`ID`); it does not replace workflow transitions, lease fencing, approval authority, or operation specifications. Portable fake-gateway work is allowed. Native-call interruption, session recovery, and live-state observations remain `BLOCKED - Windows validation required`.

## 1. Invariants

1. No PowerFactory write occurs before a committed immutable write-ahead `INTENT` for that exact target attribute and operation.
2. A timeout, disconnect, restart, or missing response never proves that a started native call did not take effect.
3. Restart never replays a started operation. Idempotency uses the original persisted operation record.
4. Reconciliation uses fresh bounded live observations through the serialized owner; caches, names, and handles cannot resolve uncertainty.
5. Unknown, ambiguous, unavailable, divergent, stale, or unauthorized state blocks further live work for the affected workflow and workspace.
6. Reconciliation disposition and recovery admission increment the scoped workflow version under `ID`; audit-only events do not.

## 2. Durable Evidence

Each consequential operation has an operation ID, workflow ID, idempotency key, expected workflow version, correlation ID, request digest, and serialized-owner submission record. Later facts append observations; they never overwrite prior evidence.

### 2.1 Write-Ahead Intent

An `INTENT` is committed before the owner invokes `write_attribute`. It contains operation/workflow/authorization/lease/fencing/workspace IDs; product UUID; verified locator evidence version; attribute selector and unit; expected-before and proposed-after canonical values; configuration key; live-state fingerprint; workspace revision; request digest; policy/specification versions; owner/session identity; attempt number; creation time; and an `intent_digest` that excludes secrets and live handles.

One intent represents one target attribute. A multi-target command commits one intent per target before that target's native call. The persistence transaction commits before submission. Failure before commit means no call was admitted. Failure after commit but before a durable observation requires reconciliation.

### 2.2 Observation and Reconciliation

An observation records attempt ID, source (`owner_return`, `live_read`, or `recovery`), bounded result/exception category, observed value when available, fresh configuration/fingerprint evidence, timestamp, and sanitized diagnostic reference. A reconciliation record binds an intent to its latest classification, evidence observation IDs, operator disposition if any, and completion time. Both are append-only and exclude PowerFactory handles, raw vendor objects, secrets, unrestricted paths, and unbounded messages.

## 3. Classification

After authorized ownership recovery, the service reacquires the exclusive context lease, verifies its fencing token, re-resolves the target under `ID`, recomputes configuration/fingerprint checks, and performs a fresh live read.

| Classification | Required live evidence | Consequence |
| --- | --- | --- |
| `BEFORE` | value equals expected-before; identity, context, and authorization bindings are valid | no effect observed; a new workflow transition may decide retry |
| `AFTER_OBSERVED` | value equals proposed-after; identity and context are valid | effect observed but actor attribution remains unknown; never continue a partial operation automatically |
| `DIVERGED` | inspectable value equals neither expected value, or identity/context/fingerprint changed | quarantine workflow/workspace; manual review required |
| `UNAVAILABLE` | engine, context, lease, identity, locator, authorization, read, unit, or persistence evidence cannot be verified | quarantine workflow/workspace; manual review required |

Quantity comparisons use exact canonical values and units unless the accepted operation specification declares a named verification tolerance. Rounding, unit guessing, partial reads, and unverified locators cannot establish `BEFORE` or `AFTER_OBSERVED`. Recovery never writes merely because it classified an intent.

## 4. Restart Behavior

Before DEP permits `READY`, startup loads all queued, in-flight, timed-out, and unreconciled operations. It marks never-started queued work `CANCELLED_BEFORE_START`; preserves every started/uncertain operation and its idempotency key; enters `SAFE_MODE` or `QUARANTINED` while live recovery is required; serially reconciles uncertain writes only through the DEP owner; and publishes `READY` only with no unresolved, quarantined, ambiguous-owner, or cleanup-required record.

A client timeout returns status only and does not cancel a started native call or allow a second execution. Lost owner health, persistence failure after submission, service exit, or unknown native outcome creates recovery-required state. Restart preserves request digest, attempt history, authorization-consumption state, fencing evidence, and source operation ID. A duplicate idempotency key returns stored status/result; a new key cannot bypass quarantine.

## 5. Quarantine and Manual Review

`DIVERGED` and `UNAVAILABLE` immediately quarantine the workflow and workspace. `AFTER_OBSERVED` remains non-admissible for dependent writes until the workflow table records an explicit next state. A DEP-quarantined service admits no new live work, but may serve authenticated health, status, audit evidence, and safe cached reads.

An authenticated local principal independent of the agent/MCP caller may record only these append-only dispositions:

| Disposition | Effect |
| --- | --- |
| `confirm_before` | records reviewed evidence; does not replay a write |
| `confirm_after_observed` | records postcondition evidence; does not attribute actor or continue automatically |
| `abandon` | closes the workflow branch without live mutation |
| `authorize_compensation` | starts a new independently authorized rollback workflow; never changes the original intent |

Manual review cannot relabel `DIVERGED` or `UNAVAILABLE` without fresh evidence.

## 6. Audit

Append an audit event before and after every consequential step. It records workflow/operation/correlation IDs, actor class, expected/resulting durable state, request or intent digest, authorization/fencing references, and sanitized evidence reference. Events distinguish `requested`, `intent_committed`, `submitted`, `owner_returned`, `client_timed_out`, `recovery_started`, `live_observed`, `classified`, `quarantined`, `manual_disposition`, and `reconciled`.

No event claims write success until a live postcondition is observed. A native return is an observation, not verified final state. The history reconstructs requested, authorized, attempted, observed, classified, and manually resolved work without confidential model data or credentials.

## 7. Executable Recovery Cases

Portable tests use fixed identifiers, a deterministic fake owner, temporary SQLite, and bounded failure injection. They prove product logic only.

| Case | Injected point | Required durable result |
| --- | --- | --- |
| `REC-001` | before intent commit | no native call or intent; failed before start |
| `REC-002` | after intent commit, before owner submission | intent exists; `BEFORE`; no automatic replay |
| `REC-003` | after native write, before observation persistence | `AFTER_OBSERVED` on matching fresh read; no causal claim |
| `REC-004` | native call produces a third value | `DIVERGED`; quarantine and manual review |
| `REC-005` | engine or target unavailable during recovery | `UNAVAILABLE`; quarantine; no replacement write |
| `REC-006` | client timeout while owner call continues | one operation/intent; later outcome persists; no cancellation/retry |
| `REC-007` | restart with queued and in-flight operations | queued canceled; in-flight reconciled; never replayed |
| `REC-008` | stale lease, changed configuration, locator ambiguity, or fingerprint mismatch | `UNAVAILABLE` or `DIVERGED`; no live write |
| `REC-009` | duplicate idempotency key across restart | original result/status; no version increment or repeat side effect |
| `REC-010` | audit/persistence failure after native submission | recovery-required quarantine; no success claim |
| `REC-011` | manual confirmation or compensation request | append-only disposition; compensation starts as a new authorized workflow |

## 8. Pending Acceptance Evidence

Windows acceptance must use an exact commit and establish blocked-native-call behavior across client/service failure, owner/session recovery, reliable fresh target reads and context restoration, and permitted termination for product-owned sessions. It must exercise interrupted writes and recovery on a non-confidential fixture. Until DEP, ID, workflow, lease, approval, and this Windows evidence are accepted, this specification remains provisional and cannot authorize live recovery behavior.
