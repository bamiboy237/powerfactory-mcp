# Identity and Revision Contract

| Field | Value |
|---|---|
| Specification key | `ID` |
| Version | `identity-and-revisions/v0.1.0` |
| Status | **PROVISIONAL — Windows evidence pending** |
| Acceptance | Not accepted; this document does not satisfy checklist item 6 |
| Governs | Buildouts 1–6 preparation; live behavior remains disabled until acceptance |

## 1. Purpose and authority

This specification makes the distinct identities in roadmap sections 4.2–4.4
executable. It governs domain schemas, the fake and PowerFactory gateways,
inventory, persistence, calculations, workflows, authorization bindings, and
pagination. It is the companion specification identified as `ID` in the
buildout dependency matrix.

The keywords **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative. This
version is intentionally provisional because Buildout 0 has not established a
stable native PowerFactory object identifier or accepted locator behavior on
Windows. Implementations MAY use this contract for platform-independent work,
but MUST fail closed where a rule depends on unresolved PowerFactory evidence.

This specification does not define deployment ownership, lease fencing,
approval authority, workflow transitions, engineering limits, or MCP transport
security. Those remain the responsibility of their own companion contracts.

## 2. Core invariants

1. `configuration_key`, `live_state_fingerprint`, `extraction_revision`,
   `workspace_revision`, `calculation_input_digest`, and `workflow_version` are
   separate typed values. No value may substitute for another.
2. A product UUID is durable application identity. A PowerFactory locator is
   versioned resolution evidence. Neither a live object handle nor a locator is
   a database primary key, workflow target, or authorization subject.
3. Display names, generated names, canonical paths, timestamps, result file
   names, and extraction revisions are never authoritative product identity.
4. An ambiguous, stale, unverified, or class-mismatched locator MUST fail
   resolution. The implementation MUST NOT guess, choose the first match, or
   silently mint an identity for the requested target.
5. Freshness is evidence about a read, not an identity and not proof that
   unrelated state is unchanged.
6. Public records MUST contain serializable identity evidence only. Process-local
   PowerFactory handles MUST remain inside the serialized gateway call that uses
   them.
7. Digests bind schema and policy versions. A change in canonicalization or
   dependency definition requires a new version prefix; it MUST NOT reinterpret
   an existing digest.

## 3. Canonical values and digests

### 3.1 Canonical encoding

All content-addressed values use `pf-agent-canonical-json/v1`:

- encode one JSON value as UTF-8 with object keys sorted by Unicode code point;
- normalize strings and object keys to Unicode NFC before encoding, and reject
  an object if normalization would create duplicate keys;
- encode booleans and null as JSON primitives;
- encode integers as JSON integers;
- encode non-integral engineering numbers as decimal strings plus an explicit
  canonical unit. Decimal strings use `^-?(0|[1-9][0-9]*)(\.[0-9]+)?$`, contain
  no exponent, remove trailing fractional zeroes and a trailing decimal point,
  and normalize negative zero to `0`; JSON floating-point values, `NaN`, and
  infinities are forbidden;
- omit no semantically meaningful field; represent an absent optional value as
  JSON null when its absence is part of the contract;
- preserve array order only where order is semantic; otherwise sort records by
  their contract-defined stable tuple before encoding;
- use lower-case enum wire values, UTC timestamps in RFC 3339 form with `Z`, and
  no insignificant whitespace.

Implementations MUST reject values that cannot be represented under these
rules. Canonicalization tests MUST use checked-in byte fixtures, not only
round-trip assertions.

### 3.2 Digest envelope

Unless a narrower contract says otherwise, a digest input is:

```json
{
  "canonicalization": "pf-agent-canonical-json/v1",
  "digest_kind": "<kind>",
  "digest_schema": "<version>",
  "payload": {}
}
```

The wire value is `<kind>:<digest-schema>:sha256:<lowercase-hex>`, where the hex
is SHA-256 over the canonical UTF-8 bytes. Digest inputs MUST exclude secrets,
credentials, live handles, wall-clock observation time, and nondeterministic
iteration order. Observation time and evidence references are stored beside the
digest. A digest match proves equality only for the declared dependency set and
schema version.

## 4. The six state identities

### 4.1 `configuration_key`

`configuration_key` identifies the selected PowerFactory operating context. Its
`configuration-key/v1` payload contains:

- a non-secret installation identifier, supported release/service-pack claim,
  extension ABI, architecture, and a product-generated opaque identifier for
  the configured profile;
- verified project provenance and project locator version;
- verified study-case locator version;
- optional operational scenario locator version;
- the ordered active variant/stage locators, including stage state where the
  admitted operation depends on it;
- the ordered active-grid locators; and
- the configuration schema version.

Order-insensitive locator lists sort by `(project_provenance, object_class,
locator_kind, locator_value)`. A context lacking exact project, study-case, or
active-grid evidence cannot be `VERIFIED`. Optional scenario and variant/stage
fields MUST explicitly distinguish “none active” from “not observed.” Profile
credentials, installation paths that reveal a username, and licence data MUST
not enter the payload.

A configuration key is recomputed after activation and post-activation
verification. It does not prove that object attributes or topology are
unchanged.

### 4.2 `live_state_fingerprint`

`live_state_fingerprint` is dependency-scoped, never model-wide by implication.
Its `live-state-fingerprint/v1` payload contains:

- `configuration_key`;
- operation type and operation-specification version;
- dependency-set name and version;
- resolved product UUID and locator-evidence version for every target;
- typed source values with canonical units and relevant relationship/configuration
  facts;
- a declared completeness state: `complete`, `conservative`, or `unsupported`;
  and
- any accepted broad-change indicator exposed by PowerFactory.

Dependency records sort by `(product_uuid, field_name, relationship_kind,
related_product_uuid)`. Only `complete` or deliberately `conservative`
fingerprints may guard a read-only preview. Mutation admission requires the
operation contract to define which completeness state is sufficient. An
`unsupported` fingerprint blocks preview authorization and execution.

If the gateway detects a broader change but cannot attribute it, every dependent
fingerprint MUST be treated as changed. Fingerprints are recomputed from current
live reads immediately before a write; a cached digest is not revalidation.

### 4.3 `extraction_revision`

`extraction_revision` is a monotonic unsigned integer scoped to one persisted
`model_context_id`. Its wire form is
`extraction-revision/v1:<model-context-uuid>:<counter>`. Counter zero means no
successful persisted extraction. The counter increments exactly once when a
transaction publishes a new extraction snapshot, including a full refresh,
accepted incremental refresh, or projection-input rebuild. Failed or rolled-back
transactions do not consume a published revision.

An extraction revision identifies persisted cache contents, not live
PowerFactory state. Two revisions may describe equal live inputs, and a cache
rebuild may increment the revision without changing any live fingerprint.
Projections record their source extraction revision and are replaceable; SQLite
records remain authoritative.

### 4.4 `workspace_revision`

`workspace_revision` is a monotonic unsigned integer scoped to a product-owned
mutation workspace UUID. Its wire form is
`workspace-revision/v1:<workspace-uuid>:<counter>`. Counter zero is the verified
initial workspace state. The counter increments in the same durable transaction
that records a verified workspace state change, candidate creation/disposition,
or reconciliation that changes the known state classification. Merely reading a
workspace does not increment it.

Unknown or partially observed effects set workspace state to `unavailable` or
`diverged`; they MUST NOT be hidden by retaining the prior trusted revision.
Pending previews and authorizations bind the exact workspace revision and are
invalid after any increment or loss of verifiability.

### 4.5 `calculation_input_digest`

`calculation_input_digest` permanently binds an immutable calculation run to
known inputs. Its `calculation-input/v1` payload contains:

- `configuration_key` and the relevant complete/conservative live-state
  fingerprint set;
- workspace UUID and revision when a candidate workspace is used;
- resolved input asset product UUIDs and locator-evidence versions;
- exact typed command settings, normalized units, calculation mode, and gateway
  adapter version;
- engineering-policy name and version;
- result-variable selection and extraction-schema version; and
- every additional input named by the calculation policy.

The digest does not contain convergence, duration, logs, or output values. Those
are outputs bound to the digest. If a required input cannot be identified, the
run MAY be retained as raw diagnostic evidence but MUST NOT become a trusted
baseline or support safety classification.

### 4.6 `workflow_version`

`workflow_version` is a compare-and-swap unsigned integer scoped to one workflow
UUID. Its wire form is `workflow-version/v1:<workflow-uuid>:<counter>`. It
increments once for each committed transaction that changes durable workflow
state, bindings, authorization consumption, reconciliation status, or recovery
disposition. Audit-only events that do not change the workflow row do not
increment it.

Every workflow command supplies an expected version and idempotency key. A new
command with a mismatched version is rejected before effects. A repeated
idempotency key returns the original operation status/result and does not
increment the version or repeat an effect. Proposal and authorization records
bind the expected version required by their workflow transition.

## 5. Product identity and PowerFactory locators

### 5.1 Product UUID

A product identity is an opaque, locally generated RFC 9562 UUIDv4 stored in
canonical lower-case form. UUID creation is independent of PowerFactory names,
paths, handles, timestamps, and extraction order. A product UUID is project
scoped through its persisted provenance relationship but is globally unique in
the product database.

The identity mapping record contains `product_uuid`, project provenance,
PowerFactory class, lifecycle state, first/last evidence references, and one or
more immutable locator versions. It MUST NOT persist a vendor object handle.

### 5.2 Versioned locator

A `powerfactory-locator/v1` is evidence with these fields:

| Field | Requirement |
|---|---|
| `locator_version_id` | Immutable product-generated UUID |
| `locator_kind` | `native_candidate` or `canonical_path_fallback` until Windows evidence accepts stronger kinds |
| `project_provenance` | Installation/profile-safe identifier plus verified project evidence |
| `object_class` | Exact expected PowerFactory class |
| `native_field` / `native_value` | Optional observed candidate; never trusted merely because present |
| `canonical_path` | Optional ordered class/name parent segments; display/resolution fallback only |
| `evidence_schema` | Locator schema and gateway adapter version |
| `observed_at` / `session_id` | Evidence metadata, excluded from locator equality |
| `trust` | `candidate`, `fallback`, `verified_native`, or `rejected` |

`verified_native` MUST NOT be emitted until Buildout 0/3 Windows cases prove the
field's uniqueness, project-copy behavior, rename/move behavior, and
deletion/recreation behavior for the supported class and PowerFactory version.
The current probe exposes read-only candidate names/full names only and makes no
stability claim. Canonical paths therefore remain fallbacks and cannot establish
durable equality after rename, move, deletion, or project copy.

### 5.3 Resolution and verification

Within one serialized gateway operation, resolution MUST:

1. verify the required context lease/fencing state under the separate lease
   contract, then recompute and compare `configuration_key`;
2. load the product mapping and an allowed locator version for the expected
   project provenance and class;
3. query only within configured cardinality and time limits;
4. require exactly one candidate; zero is `object_not_found`, more than one is
   `object_ambiguous`;
5. verify project provenance, exact class, accepted native evidence when
   available, and all locator constraints used to select the candidate. Record
   current-session runtime observations used for those checks; a process-local
   handle address or Python object identity is neither persisted nor treated as
   durable runtime identity;
6. read the operation's required identity/dependency fields from that candidate
   and compute the live-state fingerprint; and
7. use the process-local handle only before the serialized call ends.

Any mismatch is `stale_locator`, `identity_unverified`,
`configuration_mismatch`, or `class_mismatch` as applicable. A resolver MUST
NOT update or rebind a locator as a side effect of a command that requested an
existing asset. Rebinding is a separate auditable inventory/reconciliation
operation.

## 6. Tombstones and rebinding

Identity lifecycle states are `active`, `unresolved`, and `tombstoned`.

- A transient lookup failure, unavailable engine, incomplete extraction, or
  unsupported class changes `active` to `unresolved`, not `tombstoned`.
- Tombstoning requires an exact lookup with proven absence semantics or a
  complete inventory within its accepted cardinality ceiling whose coverage
  proves absence in the same project provenance. A truncated or merely sampled
  search cannot tombstone. The tombstone records the evidence revision and
  reason and is never deleted from audit history.
- Rebinding the same product UUID to a new locator version is permitted only
  when accepted native identity evidence proves it is the same live object and
  the project provenance and class still match. Rename or move alone is not
  sufficient without that evidence.
- Without accepted native equality evidence, a newly observed object receives a
  new product UUID. The implementation MAY record a non-authoritative
  `possible_successor` relationship for manual review, but MUST NOT target or
  authorize through it.
- Deletion followed by same-name or same-path recreation produces a new product
  UUID. A tombstoned UUID is never automatically resurrected.
- A project copy has distinct project provenance and distinct product UUIDs
  unless future accepted Windows evidence and an explicit import/copy policy
  define a separately audited lineage relationship. Lineage is not identity.

Any tombstone, unresolved transition, or locator rebind invalidates extraction
projections and all pending work that references the affected identity unless a
revalidation rule explicitly proves the dependency unaffected.

## 7. Freshness

Every asset/context view declares exactly one level:

| Level | Evidence |
|---|---|
| `CACHED` | Persisted extraction data; suitable for discovery only |
| `VERIFIED` | Current-session configuration and relevant identity/fingerprint checks passed within the named freshness policy |
| `LIVE` | Values were read during the current serialized operation |

Freshness records include observation time, session ID, configuration key,
dependency-set version, and evidence reference. Named freshness policies define
maximum age and required fields; there is no implicit default meaning of
“recent.” `LIVE` expires when the operation ends and may be persisted only as
historical evidence. `VERIFIED` downgrades to `CACHED` on session change,
freshness-policy expiry, configuration mismatch, relevant invalidation, engine
quarantine, or unresolvable identity.

Discovery MAY use `CACHED`. Mutation preview requires verified configuration and
live target values. Execution requires fresh live resolution and comparison of
configuration key, target values, live-state fingerprint, workspace revision,
authorization bindings, and workflow version immediately before any write.

## 8. Invalidation rules

| Event | Required invalidation |
|---|---|
| Relevant live-state fingerprint changes or cannot be recomputed | Pending proposal, execution authorization, trusted baseline, dependent classification, and live/verified freshness |
| Configuration key changes | All context-bound live/verified reads, pending proposals/authorizations, calculation admission, locators outside the new provenance, and live cursors |
| Workspace revision increments or becomes unavailable/diverged | Workspace-bound proposals, authorizations, candidate calculations, and rollback assumptions |
| Extraction revision publishes | Extraction-bound cursors and projections; not by itself live fingerprints, calculations, or authorizations |
| Calculation input digest differs | Result is a different run; prior immutable result remains valid only for its recorded inputs |
| Workflow version differs | New command is rejected by compare-and-swap; no side effect occurs |
| Product identity becomes unresolved/tombstoned or is rebound | Target-dependent previews, authorizations, cursors, and cached resolution evidence |
| Digest/canonicalization/specification version changes | Recompute under the new version; never compare unlike typed versions as equal |

Invalidation is recorded durably with reason, affected typed identities, source
evidence, and time. It does not delete immutable calculations or audit history.

## 9. Pagination cursor binding

A cursor is an opaque, authenticated server token. Its protected payload binds:

- cursor schema version and query type;
- exact `extraction_revision`, immutable result snapshot ID, or other named
  snapshot identity used by the query;
- `configuration_key` when results depend on live context;
- canonical filter digest and sort specification;
- last emitted stable sort tuple, requested/effective page size, and payload
  limit version;
- authenticated principal/audience where required by the transport contract;
- issued-at and expiry timestamps.

The stable sort tuple MUST end in product UUID or another immutable record UUID,
never display name alone. Cursor authentication failure, expiry, changed filter,
changed sort, unavailable snapshot, configuration mismatch, or bound revision
change returns a structured `cursor_invalid`/`cursor_stale` error. The server
MUST NOT silently restart pagination. Publishing a later extraction does not
invalidate a cursor bound to an immutable result snapshot, but retention may
make that snapshot unavailable and must then fail explicitly.

## 10. Identity edge-case matrix

| Case | Product identity result | Locator/result behavior |
|---|---|---|
| Duplicate names in one folder or project | Distinct UUIDs | Name-only lookup is ambiguous and fails |
| Same name in different classes | Distinct UUIDs | Exact expected class is mandatory |
| Same name in different projects/grids | Distinct UUIDs | Project provenance and relevant grid scope are mandatory |
| Rename | Preserve UUID only with accepted native equality evidence | Append locator version; otherwise old identity unresolved (tombstoned only after proven absence) and new UUID |
| Move/reparent | Preserve UUID only with accepted native equality evidence | Canonical-path fallback becomes stale |
| Project copy/import | New UUIDs in new provenance | Optional lineage is non-authoritative |
| Confirmed deletion | Tombstone old UUID | Resolution fails; history remains queryable |
| Delete then same-name/path recreation | New UUID | Never resurrect tombstone automatically |
| Class mismatch | Existing UUID unchanged | Resolution fails `class_mismatch` |
| Stale locator with one apparent match | Existing UUID unchanged | Fails unless accepted identity evidence verifies it |
| Multiple native-candidate matches | No merge or rebind | Fails `object_ambiguous`; candidate field is rejected for that scope pending review |
| Engine/session unavailable | Identity becomes or remains unresolved | No tombstone; live operation blocked |
| Extraction rebuild with equal inputs | UUIDs unchanged where mapping evidence remains valid | New extraction revision; live-state identity unchanged by inference only if separately verified |
| Unsupported object class | No fabricated durable identity claim | Report explicit unsupported/unresolved warning |

## 11. Executable acceptance cases

Acceptance requires automated tests mapped to these case IDs and sanitized
evidence tied to the exact tested commit.

| ID | Test | Expected result | Environment |
|---|---|---|---|
| `ID-CAN-001` | Canonicalize permuted equivalent fixtures | Identical bytes and typed SHA-256 digest | Any |
| `ID-CAN-002` | Feed float, non-finite, non-NFC, and unsupported values | Normalize only where specified; otherwise reject deterministically | Any |
| `ID-REV-001` | Change each of the six identity inputs independently | Only contract-defined identities/invalidation edges change | Fake/unit |
| `ID-REV-002` | Publish cache-only rebuild | Extraction revision increments; no claim that live state changed | Fake/integration |
| `ID-WF-001` | Submit stale expected workflow version | Reject before side effect and retain version | Fake/recovery |
| `ID-WF-002` | Replay an idempotency key across restart | Return original operation; no version increment or repeated effect | Fake/recovery |
| `ID-RES-001` | Resolve duplicate names and class mismatch | Structured ambiguity/mismatch; never first-match selection | Fake and Windows |
| `ID-RES-002` | Rename and move supported classes | UUID stability only where accepted native evidence proves equality | Windows |
| `ID-RES-003` | Copy project, delete, then recreate same name/path | New provenance/new UUID; old UUID tombstoned after proven absence | Windows |
| `ID-RES-004` | Make engine unavailable during identity check | Unresolved, no tombstone, no live effect | Fake failure injection and Windows |
| `ID-FP-001` | Change one declared operation dependency | Fingerprint changes and dependent proposal/authorization invalidates | Fake and Windows |
| `ID-FP-002` | Trigger an untracked broad change indicator | Conservative invalidation or explicit unsupported result | Windows |
| `ID-CALC-001` | Vary command setting, unit-normalized input, policy, or result schema | Calculation input digest changes | Fake/unit |
| `ID-CALC-002` | Repeat exact canonical calculation inputs | Same input digest; separate immutable run/output identities allowed | Fake and Windows |
| `ID-CUR-001` | Reuse cursor with changed filter, revision, sort, expiry, or signature | Structured stale/invalid error; no pagination restart | Fake/contract |
| `ID-FRESH-001` | Cross session, expire policy, or change configuration | `VERIFIED` downgrades and live admission fails until reverified | Fake and Windows |
| `ID-SER-001` | Serialize every public identity record | Versioned JSON primitives only; no PowerFactory/Python handle | Any |

Required platform-independent evidence includes canonical byte fixtures, schema
fixtures, invalidation-table coverage, fake-gateway identity cases, persistence
restart tests, cursor tamper tests, and a public-import/serialization scan.
Required PowerFactory evidence includes Buildout 0 identity capability output,
Buildout 2 resolver contract parity, and Buildout 3 live identity/pagination case
tables. Buildouts 4–6 additionally prove revision restore, calculation binding,
and workflow CAS/recovery behavior against their dependency-matrix commands.

## 12. Unresolved Windows evidence and acceptance gate

This specification remains provisional until sanitized Windows evidence answers:

1. Which PowerFactory 2026 fields, if any, are persistent and unique per
   supported object class, and what are their types and nullability?
2. Do candidate native values survive rename, move, save/reopen, process restart,
   project copy/import, deletion, and same-name recreation?
3. Can project, study case, scenario, variant/stage, grid, and asset provenance be
   read and verified without ambiguous name-only lookup?
4. What exact APIs and bounds resolve native candidates and canonical paths, and
   how do they signal zero, one, or multiple matches?
5. Is there a reliable broad model/project change indicator, and what changes
   does it cover or miss?
6. Which identity reads are valid only in an active study case or calculation
   context, and can they cause side effects?
7. Do copied projects retain candidate identifiers, and if so, which provenance
   evidence distinguishes the copy?
8. Are native identifiers stable across the pinned release/service pack and
   compatible CPython ABI, and must locator kinds be version-specific?

Evidence MUST identify the exact commit SHA, PowerFactory release/service pack,
licence capability, extension path/ABI/architecture, safe fixture, object classes,
commands, exit codes, and sanitized artifacts. Candidate names and full names
currently captured by the Buildout 0 probe are observations only; they cannot
promote a locator to `verified_native`.

To accept `identity-and-revisions/v1.0.0`, reviewers must:

1. reconcile every unresolved item with reproducible Windows evidence or an
   explicit unsupported/fail-closed rule;
2. replace provisional locator trust decisions with a supported-version/class
   matrix;
3. pass every applicable acceptance case and dependency-matrix gate for the
   implemented buildout;
4. record evidence hashes and the accepted spec revision in the dependency
   matrix; and
5. separately mark checklist item 6 only after the deployment/process-ownership
   specification is also accepted.

Until then, Buildout 1 fake/domain preparation may implement these types and
rules, but live identity resolution, persistence claims across vendor changes,
trusted calculations, and mutation admission remain disabled.

## 13. Compatibility and change control

Patch versions may clarify prose without changing canonical bytes, wire values,
or behavior. Minor versions may add optional fields or acceptance cases while
remaining provisional. Any change to canonicalization, digest membership,
identity preservation, invalidation, cursor binding, or resolution behavior is
breaking and requires a new major contract/schema version plus migration and
compatibility tests. Stored records always retain the specification and digest
schema versions under which they were created.

## 14. Roadmap traceability

| Source | Contract coverage |
|---|---|
| Runtime/state model 4.2–4.4 and ADR 12 | Six distinct identities, stable product identity versus locator evidence, freshness and invalidation |
| Core domain contracts | `ModelContext`, `AssetReference`, previews, authorization bindings, calculations, comparisons, and rollback provenance |
| Companion specification gate / matrix key `ID` | Versioned executable behavior, test cases, evidence, and explicit non-acceptance |
| Buildout 0 | Native identity and locator evidence questions |
| Buildout 1 | Typed values, schemas, deterministic fake, serialization, named bounds/errors |
| Buildouts 2–3 | Serialized resolver, bounded queries, product UUID mapping, inventory edge cases |
| Buildout 4 | Persisted extraction revisions and rebuildable projections |
| Buildout 5 | Immutable calculation input binding and result provenance |
| Buildout 6 | Workflow CAS, authorization binding, invalidation, recovery evidence |
| Adoption ledger `PFT-R01` | Reject generated names/paths as durable authoritative identity |
