# Load-Flow and Violation Policy

**Policy ID:** `load-flow-and-violation-policy/v1`

**Status:** PROVISIONAL - portable Buildout 5 preparation. PowerFactory metric mappings, engineering limits, and equivalence tolerances require Windows fixture evidence before acceptance.

## Purpose

This policy separates four concerns that must not be conflated:

1. command execution and convergence;
2. immutable result capture;
3. engineering-limit evaluation;
4. comparison materiality and result equivalence.

All calculation records are scoped to a `ModelContext.configuration_key`, `ExtractionRevision`, exact command settings, and a canonical calculation-input digest. A later run always creates a new immutable snapshot.

## Admitted Inputs

The portable implementation admits only the typed load-flow command selector, explicit `CommandSetting` values, and typed result cells from the gateway. No active-command default, free-form metric name, filesystem export, or ambient study-case state is an input.

The initial metric catalog is intentionally narrow:

| Metric | Canonical unit | Limit kind | Windows evidence required |
|---|---|---|---|
| Bus voltage | `p.u.` | inclusive lower/upper bounds | exact PowerFactory result variable and nominal-voltage basis |
| Equipment loading | `%` | inclusive upper bound | line/transformer rating selection and transformer-side mapping |

Every metric definition is versioned and carries its asset identity, source result selector, canonical unit, and limit source. A metric with no admitted definition or no limit is never classified as safe.

## Run And Snapshot Rules

`run_validated_load_flow` derives its `CalculationInputDigest` from the configuration key, extraction revision, exact command selector/settings, required metric catalog, and policy ID.

- A zero command return code is `CONVERGED`; a nonzero return code is `NOT_CONVERGED`; a gateway or persistence failure is `FAILED`.
- Non-convergence is stored as a `CalculationRun` with bounded diagnostics and log references. It is not an exception-shaped result.
- A result snapshot is immutable, has a UUID, is tied to one run and one input digest, and preserves available, missing, unsupported, and non-finite result-cell statuses.
- A trusted baseline additionally requires a verified context, convergence, exact command settings, this policy version, complete required extraction, immutable storage, and no superseding baseline chosen by the caller.

## Limit Evaluation

Voltage uses lower/upper inclusive bounds. Loading uses an inclusive upper bound. Boundaries equal to the limit are not violations. Unit mismatch, non-finite values, unavailable result cells, unsupported metrics, or missing required limits produce an explicit `NOT_EVALUATED_MISSING_LIMIT` or `NOT_EVALUATED_DATA` evaluation result, never a `Violation` claiming safety.

Severity is policy data, not a calculation side effect:

- `WARNING`: positive excess at or below the configured critical margin;
- `CRITICAL`: positive excess above that margin;
- `INFO`: reserved for accepted informational findings and not used to weaken a safety limit.

The portable default catalog contains no production engineering limits. Tests provide versioned fixture limits. Production limits, rating hierarchy, transformer side selection, and any policy override require independent approval evidence in Buildout 6.

## Comparison Rules

Comparison aligns metrics by product identity and metric definition. It separately computes:

- result equivalence: every comparable value is within the named equivalence tolerance;
- materiality: a delta exceeds the named materiality threshold;
- violation trend: `NEW`, `RESOLVED`, or `UNCHANGED` by stable violation key;

Un-evaluated findings are not silently compared as resolved. A missing limit or data state remains explicitly not evaluated in both snapshots until evidence becomes available.

## Graph Binding

Result and violation overlays reference immutable run/snapshot IDs and product identities. They are derived projection data, never a write path and never a replacement for live PowerFactory state. Rebuilding overlays from SQLite snapshots must reproduce the same references and policy IDs.

## Portable Test Cases

1. Identical context, settings, policy, and typed results yield the same calculation-input digest and equivalent comparison.
2. Nonzero command return produces a persisted `NOT_CONVERGED` run without result classification.
3. Available voltage/loading values classify against fixture limits with exact boundary behavior.
4. Missing limits, unsupported values, missing cells, and non-finite cells produce not-evaluated results, never safe or resolved violations.
5. Comparison distinguishes new, resolved, unchanged, and not-evaluated findings.
6. Every persisted snapshot, run, comparison, and overlay rebuilds from SQLite without a gateway import.

## Acceptance Blockers

The following remain `BLOCKED - Windows validation required`: command setting mapping, convergence and warning semantics, result variable names, source units, bus/line/transformer metric extraction, equipment ratings, transformer-side rules, engineering limit sources, and real baseline/equivalence tolerances.
