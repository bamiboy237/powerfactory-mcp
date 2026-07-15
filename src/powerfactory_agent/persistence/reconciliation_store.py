"""Append-only write-ahead intent and crash-reconciliation evidence."""

from __future__ import annotations

from collections.abc import Iterable
import sqlite3

from powerfactory_agent.domain.reconciliation import (
    ObservationOutcome,
    ObservationSource,
    ReconciliationClassification,
    ReconciliationObservation,
    ReconciliationRecord,
    WriteAheadIntent,
)
from powerfactory_agent.domain.workflow import WorkflowRecord, WorkflowState
from powerfactory_agent.serialization import canonical_json, from_json

from .database import SQLiteDatabase


class ReconciliationIntentNotFoundError(LookupError):
    pass


class ReconciliationIntentConflictError(ValueError):
    """An intent ID is already bound to different durable evidence."""


class ReconciliationObservationConflictError(ValueError):
    """An observation ID is already bound to different durable evidence."""


class ReconciliationClassificationError(ValueError):
    """The supplied observations cannot establish the claimed classification."""


class ReconciliationStore:
    """Persist recovery facts without invoking a gateway or changing workflow state.

    This store is intentionally evidence-only.  The later workflow orchestrator
    must compose its transaction with authorization consumption, lease start,
    and state transitions before it can submit a vendor call.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def commit_intent(self, intent: WriteAheadIntent) -> WriteAheadIntent:
        """Commit one per-attribute intent before a future owner submission.

        Exact replays return the stored fact.  A changed request under the same
        intent ID is rejected before any external effect can be admitted.
        """
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT intent_json FROM reconciliation_intents WHERE intent_id = ?", (intent.intent_id,)
            ).fetchone()
            if existing is not None:
                persisted = from_json(WriteAheadIntent, existing["intent_json"])
                if persisted != intent:
                    raise ReconciliationIntentConflictError(intent.intent_id)
                return persisted

            workflow = self._workflow(connection, intent.workflow_id)
            if workflow.workflow_version != intent.workflow_version:
                raise ReconciliationIntentConflictError(
                    "intent workflow version does not match the durable workflow"
                )
            if workflow.latest_operation_id != intent.operation_id:
                raise ReconciliationIntentConflictError(
                    "intent operation does not match the workflow's current operation"
                )
            if workflow.state not in (WorkflowState.EXECUTING, WorkflowState.ROLLING_BACK):
                raise ReconciliationIntentConflictError(
                    "write-ahead intent requires an executing or rolling-back workflow"
                )
            connection.execute(
                """INSERT INTO reconciliation_intents(
                intent_id, operation_id, workflow_id, workflow_version_counter,
                execution_id, lease_id, intent_digest, created_at, intent_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    intent.intent_id,
                    intent.operation_id,
                    intent.workflow_id,
                    intent.workflow_version.counter,
                    intent.execution_id,
                    intent.lease_id,
                    intent.intent_digest.value,
                    _timestamp(intent.created_at),
                    canonical_json(intent),
                ),
            )
        return intent

    def intent(self, intent_id: str) -> WriteAheadIntent:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT intent_json FROM reconciliation_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        if row is None:
            raise ReconciliationIntentNotFoundError(intent_id)
        return from_json(WriteAheadIntent, row["intent_json"])

    def intents_for_workflow(self, workflow_id: str) -> tuple[WriteAheadIntent, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT intent_json FROM reconciliation_intents
                WHERE workflow_id = ? ORDER BY created_at, intent_id""",
                (workflow_id,),
            ).fetchall()
        return tuple(from_json(WriteAheadIntent, row["intent_json"]) for row in rows)

    def append_observation(self, observation: ReconciliationObservation) -> ReconciliationObservation:
        """Append an observation associated with one already committed intent."""
        with self.database.transaction(immediate=True) as connection:
            intent = self._intent(connection, observation.intent_id)
            if observation.operation_id != intent.operation_id:
                raise ReconciliationObservationConflictError(
                    "observation operation does not match its write-ahead intent"
                )
            existing = connection.execute(
                "SELECT observation_json FROM reconciliation_observations WHERE observation_id = ?",
                (observation.observation_id,),
            ).fetchone()
            if existing is not None:
                persisted = from_json(ReconciliationObservation, existing["observation_json"])
                if persisted != observation:
                    raise ReconciliationObservationConflictError(observation.observation_id)
                return persisted
            connection.execute(
                """INSERT INTO reconciliation_observations(
                observation_id, intent_id, operation_id, observed_at, source, outcome, observation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation.observation_id,
                    observation.intent_id,
                    observation.operation_id,
                    _timestamp(observation.observed_at),
                    observation.source.value,
                    observation.outcome.value,
                    canonical_json(observation),
                ),
            )
        return observation

    def observations(self, intent_id: str) -> tuple[ReconciliationObservation, ...]:
        self.intent(intent_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT observation_json FROM reconciliation_observations
                WHERE intent_id = ? ORDER BY observed_at, observation_id""",
                (intent_id,),
            ).fetchall()
        return tuple(from_json(ReconciliationObservation, row["observation_json"]) for row in rows)

    def record_classification(self, record: ReconciliationRecord) -> ReconciliationRecord:
        """Append a verified classification; uncertain evidence cannot be upgraded."""
        with self.database.transaction(immediate=True) as connection:
            intent = self._intent(connection, record.intent_id)
            if record.workflow_id != intent.workflow_id:
                raise ReconciliationClassificationError("record workflow does not match its intent")
            if record.workflow_version.counter < intent.workflow_version.counter:
                raise ReconciliationClassificationError("record workflow version precedes its intent")
            observations = self._observations_by_id(connection, intent.intent_id, record.observation_ids)
            self._validate_classification(intent, observations, record.classification)
            existing = connection.execute(
                "SELECT record_json FROM reconciliation_records WHERE reconciliation_id = ?",
                (record.reconciliation_id,),
            ).fetchone()
            if existing is not None:
                persisted = from_json(ReconciliationRecord, existing["record_json"])
                if persisted != record:
                    raise ReconciliationClassificationError(
                        "reconciliation ID is already bound to different durable evidence"
                    )
                return persisted
            connection.execute(
                """INSERT INTO reconciliation_records(
                reconciliation_id, intent_id, workflow_id, workflow_version_counter,
                classification, classified_at, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.reconciliation_id,
                    record.intent_id,
                    record.workflow_id,
                    record.workflow_version.counter,
                    record.classification.value,
                    _timestamp(record.classified_at),
                    canonical_json(record),
                ),
            )
        return record

    def classifications(self, intent_id: str) -> tuple[ReconciliationRecord, ...]:
        self.intent(intent_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT record_json FROM reconciliation_records
                WHERE intent_id = ? ORDER BY sequence""",
                (intent_id,),
            ).fetchall()
        return tuple(from_json(ReconciliationRecord, row["record_json"]) for row in rows)

    def _workflow(self, connection: sqlite3.Connection, workflow_id: str) -> WorkflowRecord:
        row = connection.execute(
            "SELECT record_json FROM workflow_records WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise ReconciliationIntentNotFoundError(f"workflow {workflow_id}")
        return from_json(WorkflowRecord, row["record_json"])

    def _intent(self, connection: sqlite3.Connection, intent_id: str) -> WriteAheadIntent:
        row = connection.execute(
            "SELECT intent_json FROM reconciliation_intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        if row is None:
            raise ReconciliationIntentNotFoundError(intent_id)
        return from_json(WriteAheadIntent, row["intent_json"])

    def _observations_by_id(
        self,
        connection: sqlite3.Connection,
        intent_id: str,
        observation_ids: Iterable[str],
    ) -> tuple[ReconciliationObservation, ...]:
        observations: list[ReconciliationObservation] = []
        for observation_id in observation_ids:
            row = connection.execute(
                """SELECT observation_json FROM reconciliation_observations
                WHERE observation_id = ? AND intent_id = ?""",
                (observation_id, intent_id),
            ).fetchone()
            if row is None:
                raise ReconciliationClassificationError(
                    "classification references an observation from another intent or no observation"
                )
            observations.append(from_json(ReconciliationObservation, row["observation_json"]))
        return tuple(observations)

    @staticmethod
    def _validate_classification(
        intent: WriteAheadIntent,
        observations: tuple[ReconciliationObservation, ...],
        classification: ReconciliationClassification,
    ) -> None:
        fresh = tuple(
            observation
            for observation in observations
            if observation.source in (ObservationSource.LIVE_READ, ObservationSource.RECOVERY)
            and observation.outcome is ObservationOutcome.VALUE_OBSERVED
        )
        matching_bindings = lambda observation: (
            observation.configuration_key == intent.configuration_key
            and observation.live_state_fingerprint == intent.live_state_fingerprint
            and observation.workspace_revision == intent.workspace_revision
        )
        if classification is ReconciliationClassification.BEFORE:
            if not fresh or any(
                not matching_bindings(observation) or observation.observed_value != intent.expected_before
                for observation in fresh
            ):
                raise ReconciliationClassificationError(
                    "BEFORE requires only fresh matching reads of the exact expected value"
                )
            return
        if classification is ReconciliationClassification.AFTER_OBSERVED:
            if not fresh or any(
                not matching_bindings(observation) or observation.observed_value != intent.proposed_after
                for observation in fresh
            ):
                raise ReconciliationClassificationError(
                    "AFTER_OBSERVED requires only fresh matching reads of the exact proposed value"
                )
            return
        if classification is ReconciliationClassification.DIVERGED:
            if any(
                observation.outcome is ObservationOutcome.VALUE_OBSERVED
                and observation.source in (ObservationSource.LIVE_READ, ObservationSource.RECOVERY)
                and (
                    not matching_bindings(observation)
                    or observation.observed_value not in (intent.expected_before, intent.proposed_after)
                )
                for observation in observations
            ):
                return
            raise ReconciliationClassificationError(
                "DIVERGED requires fresh evidence of a third value or changed binding"
            )
        if classification is ReconciliationClassification.UNAVAILABLE:
            if any(observation.outcome is not ObservationOutcome.VALUE_OBSERVED for observation in observations):
                return
            raise ReconciliationClassificationError(
                "UNAVAILABLE requires an unavailable or unverifiable observation"
            )
        raise ReconciliationClassificationError(f"unsupported classification: {classification}")


def _timestamp(value: object) -> str:
    return value.isoformat().replace("+00:00", "Z")  # type: ignore[union-attr]


__all__ = [
    "ReconciliationClassificationError",
    "ReconciliationIntentConflictError",
    "ReconciliationIntentNotFoundError",
    "ReconciliationObservationConflictError",
    "ReconciliationStore",
]
