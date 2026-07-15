"""Durable immutable calculation evidence and rebuildable overlay projection."""

from __future__ import annotations

from datetime import timezone

from powerfactory_agent.domain.calculations import (
    CalculationComparison,
    CalculationOverlay,
    CalculationOverlayKind,
    CalculationRun,
    ResultSnapshot,
)
from powerfactory_agent.serialization import canonical_json, from_json

from .database import SQLiteDatabase


class CalculationNotFoundError(LookupError):
    pass


class CalculationContextMismatchError(ValueError):
    pass


class CalculationStore:
    """The calculation store never imports a gateway or writes graph source state."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def record(self, run: CalculationRun, snapshot: ResultSnapshot | None = None) -> CalculationRun:
        if (snapshot is None) != (run.result_snapshot_id is None):
            raise ValueError("run and snapshot must agree about immutable result capture")
        if snapshot is not None:
            self._require_snapshot_matches_run(run, snapshot)
        recorded_at = run.completed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO calculation_runs(
                run_id, context_id, configuration_key, extraction_counter, input_digest,
                policy_name, policy_version, convergence_state, result_snapshot_id, run_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.run_id,
                    run.context_id,
                    run.configuration_key.value,
                    run.extraction_revision.counter,
                    run.calculation_input_digest.value,
                    run.policy.name,
                    run.policy.version,
                    run.convergence_state.value,
                    run.result_snapshot_id,
                    canonical_json(run),
                    recorded_at,
                ),
            )
            if snapshot is not None:
                connection.execute(
                    """INSERT INTO calculation_snapshots(
                    snapshot_id, run_id, context_id, configuration_key, extraction_counter, input_digest,
                    policy_name, policy_version, snapshot_json, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot.snapshot_id,
                        snapshot.run_id,
                        snapshot.context_id,
                        snapshot.configuration_key.value,
                        snapshot.extraction_revision.counter,
                        snapshot.calculation_input_digest.value,
                        snapshot.policy.name,
                        snapshot.policy.version,
                        canonical_json(snapshot),
                        snapshot.captured_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    ),
                )
                for overlay in build_calculation_overlays(snapshot):
                    connection.execute(
                        """INSERT INTO calculation_overlays(
                        overlay_id, snapshot_id, product_identity, overlay_kind, overlay_json
                        ) VALUES (?, ?, ?, ?, ?)""",
                        (
                            overlay.overlay_id,
                            snapshot.snapshot_id,
                            overlay.product_identity.value,
                            overlay.overlay_kind.value,
                            canonical_json(overlay),
                        ),
                    )
        return run

    def run(self, run_id: str) -> CalculationRun:
        with self.database.connect() as connection:
            row = connection.execute("SELECT run_json FROM calculation_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise CalculationNotFoundError(run_id)
        return from_json(CalculationRun, row["run_json"])

    def snapshot(self, snapshot_id: str, *, expected_configuration_key: str | None = None) -> ResultSnapshot:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT configuration_key, snapshot_json FROM calculation_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise CalculationNotFoundError(snapshot_id)
        if expected_configuration_key is not None and row["configuration_key"] != expected_configuration_key:
            raise CalculationContextMismatchError("snapshot configuration key does not match requested context")
        return from_json(ResultSnapshot, row["snapshot_json"])

    def record_comparison(self, comparison: CalculationComparison) -> CalculationComparison:
        baseline = self.snapshot(comparison.baseline_snapshot_id)
        candidate = self.snapshot(comparison.candidate_snapshot_id)
        if baseline.configuration_key != candidate.configuration_key:
            raise CalculationContextMismatchError("comparisons require matching configuration keys")
        if baseline.policy != comparison.policy or candidate.policy != comparison.policy:
            raise ValueError("comparison policy must match both snapshots")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO calculation_comparisons(
                comparison_id, baseline_snapshot_id, candidate_snapshot_id, comparison_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    comparison.comparison_id,
                    comparison.baseline_snapshot_id,
                    comparison.candidate_snapshot_id,
                    canonical_json(comparison),
                    comparison.compared_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                ),
            )
        return comparison

    def comparison(self, comparison_id: str) -> CalculationComparison:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT comparison_json FROM calculation_comparisons WHERE comparison_id = ?", (comparison_id,)
            ).fetchone()
        if row is None:
            raise CalculationNotFoundError(comparison_id)
        return from_json(CalculationComparison, row["comparison_json"])

    def rebuild_overlays(self, snapshot_id: str) -> tuple[CalculationOverlay, ...]:
        """Recompute the projection from immutable SQLite snapshot data."""
        return build_calculation_overlays(self.snapshot(snapshot_id))

    def overlays(self, snapshot_id: str) -> tuple[CalculationOverlay, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT overlay_json FROM calculation_overlays WHERE snapshot_id = ? ORDER BY overlay_id", (snapshot_id,)
            ).fetchall()
        return tuple(from_json(CalculationOverlay, row["overlay_json"]) for row in rows)

    @staticmethod
    def _require_snapshot_matches_run(run: CalculationRun, snapshot: ResultSnapshot) -> None:
        if run.result_snapshot_id != snapshot.snapshot_id or run.run_id != snapshot.run_id:
            raise ValueError("snapshot must bind the exact run and snapshot ID")
        if (
            run.context_id != snapshot.context_id
            or run.configuration_key != snapshot.configuration_key
            or run.extraction_revision != snapshot.extraction_revision
            or run.calculation_input_digest != snapshot.calculation_input_digest
            or run.policy != snapshot.policy
        ):
            raise CalculationContextMismatchError("snapshot scope does not match calculation run")


def build_calculation_overlays(snapshot: ResultSnapshot) -> tuple[CalculationOverlay, ...]:
    """Create deterministic derived references; this is not a graph write operation."""
    evaluations = {item.definition_id: item for item in snapshot.evaluations}
    overlays: list[CalculationOverlay] = []
    for metric in snapshot.metrics:
        definition = metric.definition
        result_id = f"result:{snapshot.snapshot_id}:{definition.asset_identity.value}:{definition.definition_id}"
        overlays.append(
            CalculationOverlay(
                result_id,
                CalculationOverlayKind.RESULT,
                definition.asset_identity,
                snapshot.run_id,
                snapshot.snapshot_id,
                snapshot.policy,
                definition.definition_id,
                None,
            )
        )
        violation = evaluations[definition.definition_id].violation
        if violation is not None:
            overlays.append(
                CalculationOverlay(
                    f"violation:{snapshot.snapshot_id}:{violation.violation_key}",
                    CalculationOverlayKind.VIOLATION,
                    definition.asset_identity,
                    snapshot.run_id,
                    snapshot.snapshot_id,
                    snapshot.policy,
                    definition.definition_id,
                    violation.violation_key,
                )
            )
    return tuple(sorted(overlays, key=lambda item: item.overlay_id))


__all__ = [
    "CalculationContextMismatchError",
    "CalculationNotFoundError",
    "CalculationStore",
    "build_calculation_overlays",
]
