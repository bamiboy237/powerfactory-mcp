"""Pure calculation overlay projection derived from a result snapshot.

``build_calculation_overlays`` is a deterministic transformation of a
``ResultSnapshot`` into rebuildable ``CalculationOverlay`` references. It
accesses no SQLite, no gateway, and no store; it is importable by both
persistence and graph operations without coupling them. Move only this pure
projection here; store access stays in the persistence layer.
"""

from __future__ import annotations

from powerfactory_agent.domain.calculations import (
    CalculationOverlay,
    CalculationOverlayKind,
    ResultSnapshot,
)


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


__all__ = ["build_calculation_overlays"]