# Deployment and Process Ownership Specification

| Field | Value |
|---|---|
| Specification key | `DEP` |
| Specification ID | `deployment-and-process-ownership/v0.1.0` |
| Status | **PROVISIONAL — Windows evidence pending** |
| Governing decisions | Roadmap ADR 2 and ADR 9 |
| Initial target | PowerFactory 2026 external-engine mode |
| Acceptance prerequisite | Accepted Buildout 0 evidence at an exact commit |

This specification defines the lifecycle boundary between launchers, the local
service, the serialized PowerFactory owner, and the PowerFactory application
session. It is deliberately provisional. Portable behavior may be implemented
and tested against fakes, but this specification MUST NOT be accepted and live
PowerFactory behavior MUST NOT be enabled until the Windows decision gates in
this document and the buildout dependency matrix pass.

Normative terms `MUST`, `MUST NOT`, `SHOULD`, and `MAY` describe product
requirements. Statements marked **Windows decision point** are candidate
contracts, not claims about the vendor runtime or Windows installation.

## Scope

This specification governs:

- ownership of the local service and PowerFactory application session;
- singleton launch, authenticated reuse, and stale-owner handling;
- startup, readiness, shutdown, and crash recovery;
- attached and product-owned session cleanup;
- serialized execution and the thread-to-process isolation decision;
- queue, client-response, engine-health, and shutdown timeout semantics;
- in-flight operation durability, idempotency, and quarantine;
- deployment configuration, credentials, process metadata, and redaction.

It does not define object identity, context leases, approval authority,
engineering operations, workflow transitions, or mutation reconciliation in
full. Their companion specifications own those contracts. This specification
only defines the process-level behavior they depend on.

## Evidence classification

### Proven locally

The repository currently has portable tests and fakes that establish candidate
behavior for the Buildout 0 lifecycle probe:

- adapter construction has no vendor import or application-acquisition side
  effect;
- lifecycle stages are explicit and cleanup is attempted after failures;
- attached cleanup does not issue `PostCommand("exit")` and attempts to restore
  or deactivate only context acquired by the probe;
- explicitly product-owned cleanup issues the candidate exit command at most
  once;
- probe output is deterministic, bounded, and sanitized;
- the `powerfactory` module is isolated to the Windows adapter boundary.

These results prove product-side logic only. They do not prove that a real
PowerFactory process was attached, created, restored, terminated, or recovered.

### Pending empirical Windows evidence

The following remain unproven until exercised on the supported Windows
installation at an exact commit:

- the atomic singleton primitive and its installation/profile scope;
- reliable PID start-identity and stale-owner detection;
- whether `GetApplicationExt()` attaches to or creates an engine process;
- whether a session can be attributed to the product strongly enough to permit
  termination;
- whether `PostCommand("exit")` is sufficient and repeatable for a
  product-owned process;
- whether an attached session's prior context can be restored reliably;
- whether a blocked native call releases the GIL and leaves transport, health,
  and persistence services responsive;
- whether thread-owned recovery prevents orphaned or concurrent engine
  sessions after service, worker, or engine failure;
- suitable production timeout thresholds for representative fixtures;
- the user-only credential and metadata mechanism on the supported Windows
  release.

## Deployment topology and ownership

The supported topology is:

```text
one user-scoped launcher/reuse client
-> one authenticated service bound to 127.0.0.1
-> one serialized PowerFactory owner
-> one PowerFactory application session
-> one active live configuration envelope
```

The local service, not an MCP client, owns the serialized owner. MCP clients
MUST be disposable and MUST NOT import `powerfactory`, acquire an application
handle, own a worker, or terminate an engine. Disconnecting the client MUST NOT
shut down the service or cancel a native call that has started.

The serialized owner MUST be the only component that holds the application
handle or receives raw PowerFactory objects. Every vendor call, including
reads, activation checks, logging calls, and cleanup, MUST pass through the same
owner. Raw vendor handles MUST NOT cross its thread or process boundary.

One live context lease may use the session at a time. Serialization of
individual calls does not replace the separate workflow-level lease and fencing
contract.

## Singleton scope and authenticated reuse

### Scope key

Each service instance MUST derive a deterministic `singleton_scope_key` from:

1. a canonical installation identity for the selected PowerFactory release and
   service pack;
2. the selected PowerFactory user-profile identity;
3. the product deployment identity; and
4. the operating-system user identity.

Secrets, raw credentials, and customer project names MUST NOT contribute to or
appear in the serialized scope key. The canonicalization algorithm and its
version MUST be persisted. A changed canonicalization version creates a
different scope and therefore requires an explicit migration check rather than
silent parallel startup.

The initial product supports at most one service for a scope key. Different
scope keys are not evidence that the selected licence or PowerFactory
installation safely supports concurrent sessions.

### Lock authority

Before binding the service endpoint or acquiring an application handle, the
launcher MUST acquire an atomic OS-level ownership primitive for the scope key.
The primitive, not a PID file or discovery record, is authoritative for service
ownership. The owner MUST retain it for the service lifetime.

**Windows decision point:** select and prove the concrete primitive, naming
rules, security descriptor, abandonment behavior, and interaction with process
crashes. A user-only locked file or named mutex is a candidate; neither is
accepted by this document.

### Process metadata

The owner MUST publish bounded, user-only metadata atomically after lock
acquisition. Metadata MUST contain only:

- schema version;
- singleton scope-key digest and canonicalization version;
- service instance UUID;
- PID plus a process-start identity that detects PID reuse;
- service endpoint and transport protocol version;
- credential-store reference, never the credential value;
- lifecycle state and last successful health timestamp;
- selected gateway implementation and sanitized environment fingerprint;
- creation and last-update timestamps.

Metadata is diagnostic and discoverable; it MUST NOT authorize reuse,
termination, lock stealing, or engine ownership by itself.

### Duplicate-launch algorithm

A launcher that cannot acquire the scope lock MUST:

1. read and schema-validate the metadata using bounded I/O;
2. verify the recorded PID and process-start identity without treating that as
   sufficient proof of ownership;
3. load the credential from the protected local credential source;
4. perform an authenticated health challenge using a fresh nonce;
5. verify the returned instance UUID, scope digest, protocol version, lifecycle
   state, and nonce binding; and
6. connect to the existing service when the response is compatible.

An authentication failure, incompatible identity, malformed response, or
health timeout MUST NOT cause a second service or PowerFactory session to
start. The launcher MUST return an owner-unavailable diagnostic and leave the
existing lock untouched.

If the atomic primitive is successfully acquired and old metadata remains, the
metadata is stale. The new owner MAY replace it only after crash-recovery
admission succeeds. It MUST NOT infer that the previous PowerFactory process is
absent merely because the service lock was released.

## Authentication, configuration, and redaction

The service MUST bind to `127.0.0.1` and require authentication on health,
status, shutdown, and application endpoints. Loopback reachability and OS user
identity are not substitutes for authentication. HTTP `Origin` MUST be
validated when present according to the transport specification.

A generated bearer credential or equivalent challenge secret MUST have at
least 256 bits of cryptographic entropy. Production credential values MUST NOT
appear in command-line arguments, process metadata, ordinary configuration,
URLs, exception text, or logs. Configuration MAY contain only a reference to a
user-only secret file or OS credential-store entry. Environment-variable
credential values are limited to explicit probes and tests and are not the
production deployment contract.

Deployment configuration MUST be schema-versioned and reject unknown security
or ownership fields. It MUST name:

- PowerFactory installation and profile selectors;
- gateway version and supported ABI selector;
- singleton metadata, lock, database, artifact, and log locations;
- credential-store reference;
- project and operation allowlists;
- all timeout values defined below;
- payload, queue, and log bounds;
- startup safe-mode and operator-recovery policies.

Credential, configuration, metadata, database, and log locations MUST be
restricted to the current OS user. Startup MUST fail closed if required
permissions cannot be established or verified.

Structured logs MUST use correlation, operation, workflow, and service-instance
IDs. Redaction MUST cover authorization headers, cookies, passwords, tokens,
secret-store values, licence/server details, sensitive user names, confidential
project/study-case names, unrestricted filesystem paths, raw model dumps,
unbounded results, and vendor object representations. Logs MAY retain bounded
return codes, stage names, counts, units, sanitized aliases, hashes, capability
booleans, and exception categories needed for diagnosis. Redaction failure
MUST fail closed before a record is emitted.

## Lifecycle state machine

The service lifecycle states are:

```text
NEW
-> ACQUIRING_SINGLETON
-> STARTING
-> READY | SAFE_MODE | QUARANTINED | STARTUP_FAILED
-> STOPPING
-> STOPPED | QUARANTINED
```

`READY` admits operations allowed by policy. `SAFE_MODE` serves authenticated
health, status, evidence, and explicitly safe cached/read-only functions but
admits no live mutation. `QUARANTINED` admits no new live PowerFactory work.
`STARTUP_FAILED` and `STOPPED` do not expose an application handle.

### Startup

Constructors MUST be side-effect free. Explicit startup MUST execute these
ordered checkpoints and persist each result before advancing:

1. validate configuration schema, paths, permissions, and credential source;
2. derive the versioned scope key and acquire singleton ownership;
3. inspect stale metadata and unresolved durable operation/session records;
4. create new service metadata and an authenticated local endpoint;
5. create the serialized owner without importing the vendor module;
6. validate the selected release, ABI, architecture, and module path;
7. import the vendor module inside the owner;
8. acquire and classify the application session;
9. verify required capabilities and an admitted initial context policy; and
10. publish `READY`, `SAFE_MODE`, or `QUARANTINED` through authenticated health.

The service MUST NOT report `READY` if a checkpoint failed, cleanup is
unresolved, prior in-flight work requires reconciliation, session ownership is
ambiguous, or another owner may exist. Startup failures MUST record typed,
sanitized evidence and clean only resources actually acquired.

### Session ownership classification

Every acquired application session MUST be classified once as:

- `ATTACHED`: the process pre-existed, creation cannot be proven, ownership
  evidence is ambiguous, or deployment policy requires non-termination; or
- `PRODUCT_OWNED`: the service deliberately created the engine and retained
  evidence sufficient to distinguish that exact process and start identity.

Uncertainty MUST resolve to `ATTACHED`. Classification is immutable for that
session and MUST NOT be upgraded from metadata, PID equality, or successful API
access alone.

For `ATTACHED` sessions, shutdown and startup-failure cleanup MUST:

- never issue an engine-exit or process-termination command;
- attempt to restore the exact prior project and study-case context when the
  service changed it;
- otherwise deactivate only context activated by this service when safe;
- release local handles; and
- record every restoration, deactivation, and cleanup failure.

For `PRODUCT_OWNED` sessions, controlled shutdown MUST first restore or
deactivate acquired context, then request the empirically accepted engine-exit
operation at most once, verify process exit within the configured deadline, and
record the result. Repeating `stop()` MUST be idempotent. Failure to verify exit
MUST quarantine the session; it MUST NOT be reported as a clean stop.

No lifecycle path may require `Show()` or other GUI interaction for
correctness. GUI display is outside this specification.

### Graceful shutdown

Authenticated shutdown MUST:

1. transition atomically to `STOPPING` and reject new submissions;
2. mark queued, not-yet-started operations `CANCELLED_BEFORE_START`;
3. allow the single native call already in flight to continue;
4. wait up to `shutdown_drain_deadline` without claiming cancellation;
5. persist the returned result or required reconciliation before cleanup;
6. perform ownership-mode cleanup;
7. close transport and persistence resources;
8. remove or tombstone metadata atomically; and
9. release singleton ownership last.

If the in-flight call does not return before the shutdown deadline, the service
MUST remain `QUARANTINED`, retain singleton ownership while the process is
alive, and preserve durable recovery evidence. It MUST NOT start a replacement
owner or claim a clean shutdown. Forced termination is unavailable for attached
sessions and is a Windows-gated recovery operation for product-owned sessions.

## Serialized operations and timeouts

All live gateway operations MUST enter one bounded FIFO queue and execute one
at a time on the owner. Queue admission MUST persist an operation ID and
idempotency key before execution. Reusing an idempotency key returns the
existing operation; it MUST NOT enqueue duplicate work while the prior outcome
is non-terminal or retrievable.

The following clocks are independent configuration values:

| Setting | Starts | Meaning when exceeded | Native-call effect |
|---|---|---|---|
| `startup_deadline` | explicit service start | startup fails or quarantines with stage evidence | no implicit retry |
| `queue_deadline` | durable queue admission | cancel only if execution has not started | none; call never starts |
| `client_response_deadline` | request acceptance | client receives operation ID and `still_in_flight` status | none after start |
| `engine_health_threshold` | native-call start or last verified progress | owner/session becomes unresponsive and quarantined | no cancellation claim |
| `shutdown_drain_deadline` | transition to `STOPPING` | shutdown remains quarantined | call continues |
| `owned_exit_deadline` | accepted exit request | exit is unverified and session remains quarantined | no repeated exit request |
| `launcher_health_deadline` | duplicate-launch challenge | existing owner is unavailable | no lock stealing or launch |

Values MUST use positive integer milliseconds, be written into startup evidence,
and be adjustable without changing operation or schema semantics. This version
does not set accepted production defaults; Buildouts 0, 2, and 15 must measure
and accept them on Windows. Tests MUST inject short explicit values rather than
depend on wall-clock production defaults.

Durable operation states MUST include:

```text
QUEUED
CANCELLED_BEFORE_START
IN_FLIGHT
CLIENT_TIMED_OUT
COMPLETED
COMPLETED_AFTER_CLIENT_TIMEOUT
FAILED
ENGINE_UNRESPONSIVE
RECONCILIATION_REQUIRED
```

`CLIENT_TIMED_OUT` and `ENGINE_UNRESPONSIVE` are non-terminal. They record loss
of a waiting client or an exceeded health threshold, not proof that the native
call stopped. A later known success transitions to `COMPLETED` when the client
deadline did not expire or `COMPLETED_AFTER_CLIENT_TIMEOUT` when it did. A
known failure transitions to `FAILED`. An unknown or write-sensitive outcome
transitions to `RECONCILIATION_REQUIRED`. Status polling by operation ID MUST
work from a new client connection and after service restart.

Once `engine_health_threshold` is exceeded, the service MUST quarantine the
owner and reject all new live operations. Cached/status operations may continue
without touching PowerFactory. Quarantine may be cleared only after the
original call outcome and engine/process identity are known, required workflow
reconciliation is complete, and an authenticated operator recovery action has
succeeded. Merely receiving a late successful health response is insufficient.

## Thread-versus-process isolation gate

The initial owner MAY use one dedicated thread only for portable development
and the Buildout 2 Windows proof. All vendor calls MUST execute on that stable
thread. Transport, durable status, and watchdog work MUST not depend on the
owner thread.

Thread ownership may be accepted for live use only if Windows tests prove all
of the following at the exact candidate commit:

1. concurrent clients produce no overlapping vendor calls and no thread
   migration;
2. representative long-running and deliberately blocked native calls leave
   authenticated health, durable status, and timeout handling responsive;
3. the watchdog can quarantine without invoking PowerFactory from another
   thread;
4. late completion is recorded exactly once and cannot be resubmitted;
5. clean shutdown, application failure, service crash, and the next startup do
   not create concurrent or unusable engine sessions;
6. an unresponsive owner can be recovered without killing an attached process
   or losing required reconciliation evidence; and
7. repeated failure injection does not poison later clean runs.

If any condition fails, if a native call monopolizes the interpreter, or if a
blocked thread prevents controlled recovery, the owner MUST move to a dedicated
worker process before live use. The worker-process design MUST use authenticated
local IPC, retain the same singleton scope and durable operation IDs, serialize
all vendor calls in the child, sanitize every IPC payload, and prevent a child
restart until the previous engine/process outcome is known. Process isolation
does not authorize automatic engine termination.

## Crash, stale-owner, and restart recovery

The following rules apply before any new application acquisition:

- Lock held plus failed authenticated health means `OWNER_UNAVAILABLE`; no lock
  stealing, PID killing, or parallel startup is allowed.
- Lock acquired plus stale metadata permits metadata replacement only after the
  recorded prior service/engine identity and durable operations are inspected.
- PID existence MUST be paired with a process-start identity to reject PID
  reuse. Process name or executable path alone is insufficient.
- A surviving process previously classified `ATTACHED` MUST never be killed by
  automated recovery.
- A surviving process previously classified `PRODUCT_OWNED` remains
  quarantined until Windows-proven attribution and recovery policy authorize an
  explicit operator action.
- Durable operations left in `IN_FLIGHT`, `CLIENT_TIMED_OUT`, or
  `ENGINE_UNRESPONSIVE` MUST become `RECONCILIATION_REQUIRED` on restart unless
  their result and postcondition were durably recorded.
- Any unresolved mutation, context restoration, cleanup, or engine-identity
  question forces `SAFE_MODE` or `QUARANTINED`; it MUST NOT be guessed away.
- A new engine session MUST NOT start until the previous session is proven
  absent or an accepted recovery procedure establishes non-concurrency.

Service termination, worker termination, database failure, and machine restart
MUST each have failure-injection evidence. Destructive cleanup of PowerFactory
projects, study cases, scenarios, or variants always requires explicit operator
action and is not implied by process recovery.

## Invariants

The implementation and all later specifications MUST preserve these invariants:

1. At most one service owner is admitted for a singleton scope key.
2. Process metadata never substitutes for atomic ownership or authentication.
3. At most one vendor API call executes for the application session at a time.
4. Only the serialized owner imports `powerfactory` or handles raw vendor
   objects.
5. A client disconnect or response timeout never cancels or duplicates a
   started native call.
6. Queue expiry cancels only work that has not started.
7. Quarantine admits no new live PowerFactory operation.
8. Attached sessions are never terminated by the product.
9. Only demonstrably product-owned sessions may receive an accepted exit
   request, and that request is issued at most once.
10. Singleton ownership is released only after the session outcome and durable
    recovery state are known.
11. Unknown process, engine, operation, or cleanup state fails closed into safe
    mode, quarantine, or reconciliation.
12. Credentials, raw vendor objects, confidential model data, and unrestricted
    results never enter metadata, IPC, public responses, or logs.
13. Readiness is an authenticated, evidence-backed lifecycle state; object
    construction or PID presence cannot imply readiness.
14. No accepted deployment path requires GUI interaction.

## Executable acceptance cases

Case IDs are stable test requirements. Implementations MAY split a case across
multiple test files but MUST retain the ID in test metadata or evidence.

### Portable and fake-gateway cases

| ID | Setup and action | Required result |
|---|---|---|
| `DEP-L01` | Derive scope keys from normalized installation, profile, deployment, and OS-user fixtures. | Equal inputs produce equal versioned digests; any component change changes the scope; no secret appears. |
| `DEP-L02` | Start two launchers against a fake atomic owner and authenticated health service. | Exactly one owner/session starts; the second authenticates and reuses it. |
| `DEP-L03` | Hold the lock but return missing, forged, incompatible, and timed-out health responses. | Second launch fails with `OWNER_UNAVAILABLE`; it neither steals the lock nor starts a session. |
| `DEP-L04` | Inject failure at every startup checkpoint. | No false `READY`; only acquired resources are cleaned; evidence is typed and redacted; next clean start is possible. |
| `DEP-L05` | Submit concurrent reads and commands through the fake gateway. | FIFO trace has one stable owner thread and zero overlapping calls. |
| `DEP-L06` | Expire queue, client-response, health, shutdown-drain, and exit clocks independently. | Each clock produces only its specified transition and never claims native cancellation. |
| `DEP-L07` | Retry a timed-out request and poll it from a new client. | The same operation ID is returned; work executes once; late completion is durable. |
| `DEP-L08` | Stop attached, product-owned, unknown, and partially started fake sessions twice. | Attached/unknown never exit; owned exits at most once; stop is idempotent; cleanup failures quarantine. |
| `DEP-L09` | Exercise held lock, stale metadata, PID reuse, orphan-record, and unresolved-operation fixtures. | Recovery follows the fail-closed stale-owner rules and never kills or launches concurrently. |
| `DEP-L10` | Property-test metadata, errors, logs, and IPC/public serializers with sensitive and vendor-shaped values. | Bounds hold and prohibited content is rejected or redacted before emission. |
| `DEP-L11` | Restart from every non-terminal operation state. | Status is preserved; uncertain outcomes require reconciliation; no automatic replay occurs. |
| `DEP-L12` | Deny or weaken filesystem permissions in a portable permission adapter. | Startup fails closed without exposing credential or metadata content. |

These cases establish the provisional contract and may support continued
platform-independent buildout. They do not accept `DEP`.

### Required Windows cases

| ID | Setup and action | Evidence and decision |
|---|---|---|
| `DEP-W01` | Run competing launchers for the same real installation/profile scope. | Prove atomic ownership, user isolation, authenticated reuse, one service, and one engine session. |
| `DEP-W02` | Acquire applications in clean, pre-existing, failed-start, and repeated-start conditions. | Determine attach/create semantics and the evidence sufficient for immutable session classification. |
| `DEP-W03` | Repeat attached and demonstrably product-owned cleanup after success and injected stage failures. | Prove restoration/deactivation, accepted exit behavior, exit-once, process absence where owned, and no poisoned next run. |
| `DEP-W04` | Execute representative long-running and deliberately unresponsive native calls while polling health/status. | Decide whether thread ownership satisfies every thread-versus-process criterion and record GIL/service responsiveness. |
| `DEP-W05` | Terminate client, service, worker where applicable, and engine at controlled failure points. | Prove stale-lock handling, PID-reuse defense, orphan detection, quarantine, and non-concurrent restart. |
| `DEP-W06` | Inspect lock, metadata, configuration, credential, database, artifact, and log permissions as another local principal where permitted. | Accept a concrete Windows user-only storage/ACL and credential mechanism. |
| `DEP-W07` | Exercise explicit timeout configurations against the safe fixture. | Record distributions, select production values, and prove timeout/idempotency transitions without duplicate execution. |
| `DEP-W08` | If any thread criterion fails, repeat ownership, timeout, crash, and restart cases through authenticated worker-process IPC. | Accept process isolation only when contract parity and non-concurrent recovery pass. |

Every Windows artifact MUST identify the exact commit SHA, PowerFactory release
and service pack, licence capability, `powerfactory.pyd` path, CPython ABI and
architecture, sanitized fixture ID, timeout configuration, session
classification, exit codes, and artifact hashes. Manual GUI action, stale
processes, hangs, crashes, fixture changes, or command deviations MUST be
reported.

## Buildout gates

| Buildout | Use of this specification |
|---:|---|
| 0 | Produces the lifecycle, ownership, capability, ABI, and cleanup evidence needed to revise and accept `DEP`. Current status remains **BLOCKED - Windows validation required**. |
| 1 | May implement portable types, state machines, fake ownership, and fake timeout behavior against this provisional version. Buildout acceptance still requires accepted `DEP`, `ID`, and `DOM`. |
| 2 | Implements the real singleton service and gateway. Acceptance requires all applicable `DEP-L*` and `DEP-W*` cases plus the dependency-matrix thread/process gate. |
| 15 | Repeats and hardens crash, stale-owner, quarantine, credential, permission, redaction, and controlled-restart evidence. Controls defined here are prerequisites, not deferred additions. |
| 16 | Packages only release/service-pack/ABI/architecture combinations with retained Buildout 0 and `DEP` evidence; unsupported combinations must be refused. |

## Acceptance decision

`DEP` may move from provisional to accepted only when:

1. Buildout 0 has accepted evidence for the exact supported Windows
   combination;
2. every `DEP-L*` case passes;
3. `DEP-W01` through `DEP-W07` pass, and `DEP-W08` passes if the process gate is
   triggered;
4. the concrete lock, credential, ACL, process-attribution, exit, timeout, and
   isolation decisions replace all Windows decision points in a versioned
   revision;
5. no unresolved result permits duplicate sessions, termination of an attached
   process, duplicate operation execution, secret leakage, or false readiness;
6. the dependency-matrix evidence row identifies commands, artifacts, hashes,
   fixture/environment ID, and the accepted thread-or-process decision; and
7. a reviewer explicitly accepts that revision.

Until then, checklist item 6 remains unchecked and real-engine dependent
behavior remains disabled or marked **BLOCKED - Windows validation required**.

## Unresolved Windows evidence register

| Decision | Required observation | Fallback if unproven |
|---|---|---|
| Atomic singleton primitive | Crash, abandonment, competing-launcher, ACL, and scope behavior | Do not launch the live service. |
| Application attach/create classification | Process identity before/after acquisition across clean and pre-existing states | Classify as attached; never terminate. |
| Attached context restoration | Project/study-case before/after success and every failure stage | Quarantine and require operator restoration. |
| Product-owned exit | Accepted API call, return evidence, process exit, repeatability, and next-run health | Quarantine; do not report clean shutdown or repeat exit blindly. |
| Native-call/GIL behavior | Health, status, watchdog, persistence, and shutdown responsiveness during a blocked call | Move owner to authenticated worker-process IPC. |
| Stale engine recovery | Service/worker crash with surviving engine and exact start identity | Safe mode; no new session until explicit recovery. |
| Windows credential and ACL mechanism | Access tests for same user and disallowed local principals | Refuse production startup. |
| Production timeout values | Measured lifecycle and operation durations, including slow and failed cases | Require explicit conservative configuration and retain quarantine behavior. |
| Worker-process termination, if selected | Child/service/engine failure matrix without concurrent sessions or lost evidence | Disable the live gateway and retain fake/read-only development. |

## Traceability

This specification refines roadmap sections 3 and 4.1, the companion
specification gate, ADR 2, ADR 9, and Buildouts 0, 1, 2, 15, and 16. The
dependency matrix remains authoritative for buildout completion. The Buildout 0
Windows handoff remains authoritative for the current probe commands and
evidence return. Adoption-ledger entry PFT-07 supplies a cleanup pattern only;
it does not prove product process ownership or Windows lifecycle behavior.
