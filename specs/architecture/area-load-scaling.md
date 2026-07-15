# Area Load-Scaling Specification

| Field | Value |
| --- | --- |
| Specification key | `AREA` |
| Specification ID | `area-load-scaling/v1` |
| Status | **PROVISIONAL - portable Buildout 7 preparation; PowerFactory target and relationship evidence pending** |
| Governing buildout | Buildout 7 - Area Load-Scaling Preview |
| Acceptance prerequisite | Accepted `ID`, `LEASE`, `WF`, domain schemas, exact-commit Windows capability evidence, and Buildout 7 dependency-matrix gate |

## 1. Purpose and authority

This contract defines the only admitted first high-level engineering operation:
`preview_area_load_scaling(area, percentage)`. It produces an immutable,
explainable proposal to scale direct load P/Q setpoints in one area. It never
applies, queues, disables, creates, activates, or otherwise changes a
PowerFactory object, project setting, command, controller, characteristic,
profile, scenario, variant, study case, or workspace.

The keywords **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative.
`ID` owns identity, revision, canonicalization, and live-state-fingerprint
rules. `LEASE` owns the live preview envelope and fencing. `WF` owns durable
workflow transition and authorization-wait behavior. `AUTH` and `REC` govern
later execution and recovery; neither is an authorization to execute this
preview. This specification defines neither a generic attribute write nor an
override-remediation path.

This version is intentionally fail-closed. The PowerFactory 2026 class,
attribute, control, profile, and area-membership mappings below are candidate
adapter mappings, not accepted native claims. A fake gateway may model them for
portable tests. A real gateway MUST exclude a target whose mapping or evidence
is not accepted by the required Windows capability matrix.

## 2. Request, output, and non-mutation invariant

A request contains exactly:

- an `area` as a `ProductIdentity`, or a versioned locator resolved to one
  unambiguous `AssetKind.AREA` product identity under `ID`;
- `percentage` as a finite base-10 decimal in canonical unit `%`; and
- the expected workflow/version and preview-lease/fencing values required by
  `WF` and `LEASE`.

`percentage` is a target multiplier expressed as a percent, not a signed
delta. `100 %` preserves each admitted setpoint, `110 %` increases its
magnitude by ten percent, and `0 %` proposes zero P and Q. The admitted closed
range is `[0 %, 200 %]`. Negative, non-finite, exponent-form, ambiguous-unit,
or out-of-range values are rejected before a live read or proposal record.

The operation runs only in a current `HELD_PREVIEW` lease. It reads all
selection dependencies through the serialized gateway owner, persists the
complete `ChangePreview`, records the preview workflow transition, and releases
the lease before `AWAITING_AUTHORIZATION`. A failure before persistence creates
no preview; a failure after persistence follows `WF` recovery rules. No preview
may retain a lease while awaiting authorization.

The gateway surface used by a preview is read-only. A contract test MUST spy on
the gateway and prove that no command, activation, attribute write, controller
toggle, scenario/variant change, or other mutating primitive is dispatched.

## 3. Admitted load target profile

### 3.1 Exact class and model eligibility

The initial target profile is `direct-balanced-load/v1`:

| Property | Requirement |
| --- | --- |
| PowerFactory class | Exact candidate class `ElmLod` only. Any other class returned by a gateway is `UNSUPPORTED_LOAD_CLASS`. |
| Connection/model form | The gateway MUST positively identify the object as a balanced, steady-state direct-load model. An unbalanced, phase-specific, asymmetrical, dynamic, composite, or unknown form is excluded. |
| Service state | The load and its authoritative direct setpoint source MUST be in service. An out-of-service or unknown state is excluded. |
| Controllability | The P and Q values must be direct setpoints with no active higher-precedence source in section 5. |
| Context | Identity, locator evidence, configuration key, model context, and extraction revision must all match the request snapshot. |

`ElmLod` is a candidate PowerFactory 2026 mapping, not proof that every
`ElmLod` is balanced or directly setpoint-controlled. `ElmLodmv`, low-voltage,
unbalanced, phase-specific, composite, external-grid, generator, static
generator, transformer, and every future/unknown load class are unsupported in
this version. They MUST be returned as exclusions, never coerced into an
`ElmLod` interpretation.

Before the Windows gate accepts the adapter capability, a real gateway reports
`POWERFACTORY_CAPABILITY_UNVERIFIED` for the native class/model profile. This
means portable previews can exercise the full durable contract without making a
live PowerFactory eligibility claim.

### 3.2 Authoritative logical attributes

The product addresses logical fields, never free-form vendor attribute names:

| Logical selector | Candidate PowerFactory attribute | Canonical unit | Meaning |
| --- | --- | --- | --- |
| `load.active_power_setpoint` | `plini` | `MW` | Direct active-power setpoint P |
| `load.reactive_power_setpoint` | `qlini` | `Mvar` | Direct reactive-power setpoint Q |

The candidate raw attributes are recorded only as gateway provenance. The
gateway MUST return source value, source unit, source selector/version,
conversion evidence, availability, and controlling-source evidence in one
bounded read. Missing, unreadable, non-finite, unitless, or unit-incompatible
P or Q excludes the complete load. The preview MUST NOT substitute result
variables, rated power, apparent power, power factor, defaults, cached values,
or a name-based attribute lookup.

The PowerFactory attribute names, unit behavior, and whether a load's displayed
P/Q values are the authoritative direct setpoints require Windows validation.
Until then, `plini` and `qlini` remain candidate mapping evidence only.

## 4. Arithmetic, units, rounding, and bounds

Let `r = percentage / 100` in exact `Decimal` arithmetic. For each admitted
load, convert authoritative source values to canonical units before computing:

```text
P_proposed = round_6(P_source_MW * r)
Q_proposed = round_6(Q_source_Mvar * r)
```

`round_6` quantizes to `0.000001` in the named canonical unit using
`ROUND_HALF_EVEN`. The unrounded product, rounded value, canonical unit,
source unit/value, conversion path, multiplier, rounding rule, and resulting
absolute-bound check are persisted for each target attribute. The result must
be canonicalized under `ID`; binary floating point is not an admitted input or
intermediate representation.

The default profile has inclusive per-attribute absolute bounds
`[-1000000.000000, 1000000.000000] MW` and
`[-1000000.000000, 1000000.000000] Mvar`. A deployment MAY configure stricter
bounds only through a versioned policy record included in the proposal digest;
it MUST NOT configure wider bounds under `area-load-scaling/v1`. A target whose
rounded P or Q exceeds its bound is excluded as `VALUE_BOUND_EXCEEDED` and no
partial single-attribute proposal is created.

Scaling is component-wise and sign preserving for every `r` in the admitted
nonnegative range:

- positive P/Q remain nonnegative, negative P/Q remain nonpositive, and zero
  remains zero;
- negative values are treated as recorded reverse-flow/generation-shaped
  setpoints, not silently made positive or reclassified as a different asset;
- nonzero P and Q are multiplied by the same exact ratio, so their apparent
  power, quadrant, and power-factor magnitude are preserved subject only to
  declared decimal rounding;
- P equals zero with nonzero Q has zero power factor before and after scaling;
  Q equals zero is handled normally; P and Q both zero have undefined power
  factor and that fact is recorded; and
- at `0 %`, P and Q both become zero and power factor is explicitly `undefined`;
  the preview MUST NOT derive Q from a retained power factor or retain a stale
  Q value.

The preview reports aggregate source/proposed/delta totals for P and Q as the
sum of the persisted rounded per-load values in stable target order. It MUST
NOT calculate a proposed total first and distribute it, or hide per-target
rounding in the aggregate.

## 5. Controlling sources and exclusion precedence

The operation scales only a verified direct P/Q source. It never disables,
edits, materializes, or attempts to infer the output of a controller,
characteristic, time-series input, profile, or other override. The gateway
returns an independent status for P and Q and a complete list of observed
controlling-source facts.

For a non-admitted source, the exclusion reason is selected by this strict
precedence; all observed facts remain in provenance:

1. `ACTIVE_TIME_SERIES_OR_PROFILE` for an active time-series, profile, schedule,
   or externally supplied value controlling either target field.
2. `ACTIVE_CHARACTERISTIC` for an enabled characteristic/curve controlling
   either target field.
3. `ACTIVE_CONTROLLER` for a controller, automation, or setpoint manager
   controlling either target field.
4. `OVERRIDE_STATE_UNAVAILABLE` when the gateway cannot determine control state
   completely or reports an unknown competing source.
5. `DIRECT_SETPOINT_UNAVAILABLE` when no verified direct P/Q source remains.

A load with one controlled component is excluded as a whole. This prevents a
partial P-only or Q-only change from changing intended power-factor behavior.
If multiple sources are active, the highest listed reason is the stable summary
reason and every source is reported in its stable `(source_kind, product_id or
opaque source key)` order. A future accepted control precedence requires a new
AREA version; it cannot be changed by adapter heuristics.

## 6. Area resolution and membership

### 6.1 Area resolution

The request resolves an area before any target selection. A product identity is
admitted only when it names exactly one current `AssetKind.AREA` in the selected
model context. A locator is admitted only when `ID` resolves it in one
serialized read to that same identity, exact expected class, project provenance,
and configuration key. Name, display label, path-prefix, first-match, or
cross-context resolution is prohibited.

An absent area is `AREA_NOT_FOUND`; a class/context mismatch is
`AREA_CONTEXT_MISMATCH`; and one or more candidate areas is
`AREA_AMBIGUOUS`. Each rejects the request before target reads and produces no
preview. Native locator trust is currently provisional, so live locator
resolution remains blocked until the relevant Windows identity evidence is
accepted.

### 6.2 Membership source and edge cases

The only candidate membership relation in v1 is a direct, verified relationship
from the current immutable graph snapshot: the load's `area_identity` equals
the resolved area product identity. It must be scoped to the exact
`configuration_key`, `model_context_id`, and `extraction_revision` recorded by
the preview. The raw PowerFactory relationship selector, object identities,
class mapping, and verification observation are required provenance.

The following rules are deliberate:

| Case | v1 behavior |
| --- | --- |
| Direct, unique membership | Candidate for the remaining filters. |
| No area relationship | Exclude `NOT_A_MEMBER`. |
| Multiple distinct area memberships or contradictory current observations | Exclude `AREA_MEMBERSHIP_AMBIGUOUS`. |
| Nested area / child-area relationship | Do not recurse or inherit members. Report `NESTED_AREA_UNSUPPORTED`; only independently direct members are considered. |
| Missing, stale, incomplete, or unverified membership evidence | Exclude `AREA_MEMBERSHIP_UNAVAILABLE`. |
| Direct member disconnected from the current in-service electrical component | Exclude `DISCONNECTED_FROM_ACTIVE_NETWORK`. |
| Connectivity unavailable/incomplete | Exclude `CONNECTIVITY_UNAVAILABLE`; do not infer connectivity from area hierarchy. |
| Out-of-service load or relationship | Exclude `OUT_OF_SERVICE`. |

Area membership is not inferred from names, folders, electrical adjacency,
zones, terminals, containment, or prior previews. Direct membership and
connectivity are distinct facts. The current graph topology contract marks
native area/zone and connectivity extraction as Windows-pending; therefore a
real preview must fail closed when the gateway cannot supply the required
verified facts.

## 7. Deterministic selection and preview construction

With a valid preview lease, the service reads the resolved area, current
configuration key, graph snapshot, membership facts, connectivity facts, and
all candidate P/Q/control-state dependencies. It creates one target or
exclusion record for every discovered candidate load. Discovery and evaluation
are bounded by configured maximum candidate count; exceeding that bound
returns a typed limit error with no incomplete preview.

Records are evaluated and persisted in canonical ascending tuple order:

```text
(product_uuid, logical_attribute_selector, exclusion_reason, source_selector)
```

Target identity collisions, duplicate read records, conflicting source values,
or a different evaluation result for the same stable identity are
`TARGET_EVIDENCE_AMBIGUOUS` and exclude that load. The service MUST NOT rely on
vendor iteration order, graph traversal order, display name, timestamps, or a
previous cache entry.

One `ChangePreview` is immutable and contains at least:

- preview/workflow/operation IDs, request digest, operation specification
  `area-load-scaling/v1`, selection-profile version, arithmetic/rounding/bounds
  policy versions, and gateway adapter version;
- area identity and locator-resolution evidence; exact percentage/multiplier;
- `configuration_key`, dependency-scoped `live_state_fingerprint`, model
  context ID, `extraction_revision`, workspace UUID/revision where applicable,
  and expected `workflow_version`;
- selection query/configuration key, maximum candidate/summary/page limits,
  graph snapshot/content digest, raw membership/connectivity evidence, and all
  source attribute/control observations;
- one immutable `ChangePreviewEntry` per target attribute with original,
  proposed, delta, units, conversion, rounding, bound, and power-factor facts;
- one immutable exclusion per rejected load with stable reason, all observed
  facts, and an operator-safe display label; and
- counts and P/Q aggregates derived from the persisted entries, not mutable
  live data.

The live-state fingerprint dependency set includes the resolved area relation,
membership/connectivity facts, in-service state, class/model eligibility,
authoritative P/Q values and units, control/override facts, relevant policy
versions, configuration key, graph/extraction evidence, and configured limits.
It is `unsupported` whenever any required fact is unavailable. An unsupported
fingerprint blocks proposal authorization and later execution.

## 8. Bounded summary, details, and provenance

The first response returns a summary only. It includes preview ID/digest,
state, area reference, percentage, selected/excluded counts, P/Q source and
proposed totals, warning categories/counts, all required revisions/digests, and
at most `preview_summary_limit` representative entries. The configured limit is
recorded in the preview and has a hard maximum of 100 entries.

Details use an opaque cursor bound to the immutable preview ID, its content
digest, the fixed stable record sequence, requested detail kind (`target` or
`exclusion`), page size, and policy/schema version. A page size is 1 through
100 and cannot exceed the persisted `preview_detail_page_limit`. The cursor
must reject a missing/deleted preview, digest mismatch, expired retention,
wrong caller/authorization scope, changed policy/schema version, malformed
offset, or a request to mix target and exclusion sequences. Paging a persisted
preview never rereads PowerFactory and therefore cannot change target selection,
totals, ordering, or provenance.

All output is bounded, canonical, and secret-free. It may expose an approved
operator-safe asset label and stable product identity, but never a vendor
object handle, credential, raw controller object, unbounded characteristic
payload, or installation path. A summary includes a truncation indicator and
the details cursor when records exceed its configured size.

## 9. Exclusion and error taxonomy

Every discovered load has exactly one terminal classification: `SELECTED` or
one stable exclusion code. The v1 codes are:

```text
UNSUPPORTED_LOAD_CLASS
POWERFACTORY_CAPABILITY_UNVERIFIED
UNBALANCED_OR_MODEL_UNSUPPORTED
OUT_OF_SERVICE
NOT_A_MEMBER
AREA_MEMBERSHIP_AMBIGUOUS
AREA_MEMBERSHIP_UNAVAILABLE
NESTED_AREA_UNSUPPORTED
DISCONNECTED_FROM_ACTIVE_NETWORK
CONNECTIVITY_UNAVAILABLE
ACTIVE_TIME_SERIES_OR_PROFILE
ACTIVE_CHARACTERISTIC
ACTIVE_CONTROLLER
OVERRIDE_STATE_UNAVAILABLE
DIRECT_SETPOINT_UNAVAILABLE
SOURCE_VALUE_UNAVAILABLE
SOURCE_UNIT_UNSUPPORTED
VALUE_BOUND_EXCEEDED
TARGET_EVIDENCE_AMBIGUOUS
```

Request-level errors are distinct from exclusions: `INVALID_PERCENTAGE`,
`AREA_NOT_FOUND`, `AREA_CONTEXT_MISMATCH`, `AREA_AMBIGUOUS`,
`PREVIEW_LEASE_REQUIRED`, `STALE_FENCE`, `WORKFLOW_VERSION_CONFLICT`,
`CANDIDATE_LIMIT_EXCEEDED`, `FINGERPRINT_UNSUPPORTED`, and
`PERSISTENCE_UNAVAILABLE`. No request-level error may return a partial,
authorizable target set.

## 10. Executable acceptance cases

Portable tests use fixed UUIDs/timestamps, decimal fixtures, a spy fake
gateway, deterministic graph snapshots, a deterministic lease/workflow store,
and temporary SQLite. Windows rows use an approved non-confidential fixture,
exact commit SHA, release/service pack, licence capability, `powerfactory.pyd`
path, CPython ABI/architecture, exact commands, exit codes, sanitized logs,
and captured evidence artifacts.

| ID | Exercise | Expected result | Environment |
| --- | --- | --- | --- |
| `AREA-001` | Preview a direct balanced `ElmLod` with P/Q values in convertible units | One selected load; exact canonical P/Q values, multiplier, deltas, rounding, and stable totals | Fake/unit |
| `AREA-002` | Scale positive, negative, zero-P, zero-Q, and zero-P/zero-Q loads at 0 %, 100 %, and 110 % | Component signs and defined/undefined power-factor facts follow section 4 | Fake/unit |
| `AREA-003` | Use ties at the sixth decimal and values at percentage/absolute bounds | `ROUND_HALF_EVEN`; inclusive percentage/absolute boundaries accepted; out-of-range values rejected/excluded | Fake/unit |
| `AREA-004` | Supply non-finite, exponent-form, unitless, ambiguous-unit, negative, or greater-than-200 % percentages | Rejected before live target reads or preview persistence | Fake/contract |
| `AREA-005` | Return non-`ElmLod`, unbalanced, unknown model, out-of-service, or unverified-capability loads | One explainable exclusion per load; no approximation or partial target | Fake/contract and Windows |
| `AREA-006` | Return profile/time-series, characteristic, controller, unknown, and multiple simultaneous controlling sources | Whole-load exclusion follows the exact precedence; every observed source remains in provenance | Fake/contract and Windows |
| `AREA-007` | Resolve area by duplicate name, product identity, locator, wrong context, and stale/ambiguous locator | Only exact verified identity resolves; all ambiguous/mismatched cases fail before target reads | Fake/contract and Windows |
| `AREA-008` | Model direct, missing, nested, multiple, stale, disconnected, and unknown area membership/connectivity | Selection/exclusion matches section 6 and stable codes | Fake/integration and Windows |
| `AREA-009` | Vary vendor/graph order and replay same snapshot | Identical preview content digest, target order, exclusions, totals, and cursor pages | Fake/property |
| `AREA-010` | Exceed candidate, summary, or page bounds; tamper cursor or use cursor after preview replacement/expiry | No incomplete preview; bounded summary/details; tampered or stale cursor rejected | Fake/integration |
| `AREA-011` | Spy on every primitive while creating/paging preview | No write, command, activation, controller/profile change, or workspace mutation; pages make no live calls | Fake/contract |
| `AREA-012` | Change P/Q, control state, membership, configuration, extraction/workspace revision, policy, target set, or percentage after preview | Different fingerprint/proposal digest; old preview/authorization cannot admit execution | Fake/recovery and Windows |
| `AREA-013` | Restart after persisted preview and after lease-release/authorization wait | Exact immutable preview restores; no held lease or duplicate live read | Fake/recovery |
| `AREA-014` | PowerFactory 2026 fixture probe for `ElmLod`, `plini`, `qlini`, units, balanced-state indicator, controller/profile facts, area relation, and connectivity | Capability matrix either proves exact mapping or reports exclusions; no inferred mapping | Windows required |
| `AREA-015` | Run complete preview against Windows fixture and independently inspect before/after model state | State is byte/semantic-equivalent for admitted dependencies; provenance links every observed fact | Windows required |

## 11. Acceptance gate and unresolved evidence

This document is not accepted merely because it exists or portable tests pass.
Acceptance of `area-load-scaling/v1` requires the Buildout 7 dependency-matrix
commands to pass for an exact commit and the named companion specifications to
be accepted. Native evidence must establish, per admitted PowerFactory release,
service pack, and load model:

1. exact class/model eligibility and balanced/unbalanced indicator semantics;
2. P/Q direct-setpoint attributes, source/display units, signs, read behavior,
   and no-hidden-normalization behavior;
3. controller, characteristic, profile, and time-series detection semantics;
4. exact area relationship, nested/multi-membership behavior, in-service state,
   and connectivity interpretation;
5. identity/locator resolution under duplicate names, rename/move, copy, and
   unavailable objects; and
6. proof that all preview primitives leave the fixture and context unchanged.

Until that evidence is accepted, a real PowerFactory gateway must report the
relevant unverified or unavailable exclusions and no later mutation capability
may treat an AREA preview as execution-ready.

## 12. Traceability

- Product roadmap: Buildout 7 items 1-14 and success criteria.
- `ID`: product/locator resolution, canonical Decimal/digest rules, revisions,
  live-state fingerprint, and invalidation.
- `LEASE`: `HELD_PREVIEW` admission, fencing, and release before authorization.
- `WF`: preview persistence, authorization wait, idempotency, and recovery.
- `model-graph-topology`: immutable graph snapshot, area/zone membership, and
  connectivity evidence boundary.
- Open-source adoption ledger: PFT-02 class/query caution, PFT-03 unit
  normalization/restoration caution, PFT-04 explicit context state, PM-R01
  rejection of unrestricted mutation, and PFT-R01 rejection of name identity.
