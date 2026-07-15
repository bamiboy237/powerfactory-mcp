from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import unittest

from powerfactory_agent.domain import (
    ConfigurationKey,
    ContentDigest,
    ContextLease,
    LeaseEvent,
    LeaseEventType,
    LeaseMode,
    LeaseState,
    WorkflowVersion,
)


LEASE_ID = "11111111-1111-4111-8111-111111111111"
WORKFLOW_ID = "22222222-2222-4222-8222-222222222222"
OWNER_ID = "33333333-3333-4333-8333-333333333333"
EVENT_ID = "44444444-4444-4444-8444-444444444444"
OPERATION_ID = "55555555-5555-4555-8555-555555555555"
COMMAND_ID = "66666666-6666-4666-8666-666666666666"
CORRELATION_ID = "77777777-7777-4777-8777-777777777777"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


class LeaseContractTests(unittest.TestCase):
    def lease(self, **overrides: object) -> ContextLease:
        values: dict[str, object] = {
            "lease_id": LEASE_ID,
            "service_scope_digest": ContentDigest(f"content:v1:sha256:{'a' * 64}"),
            "configuration_key": ConfigurationKey(f"configuration-key:v1:sha256:{'b' * 64}"),
            "workflow_id": WORKFLOW_ID,
            "workflow_version": WorkflowVersion(WORKFLOW_ID, 4),
            "fencing_token": 8,
            "mode": LeaseMode.EXECUTION,
            "state": LeaseState.HELD_EXECUTION,
            "issued_at": NOW,
            "expires_at": NOW + timedelta(minutes=5),
            "owner_instance_id": OWNER_ID,
        }
        values.update(overrides)
        return ContextLease(**values)  # type: ignore[arg-type]

    def event(self, **overrides: object) -> LeaseEvent:
        values: dict[str, object] = {
            "event_id": EVENT_ID,
            "lease_id": LEASE_ID,
            "service_scope_digest": ContentDigest(f"content:v1:sha256:{'a' * 64}"),
            "configuration_key": ConfigurationKey(f"configuration-key:v1:sha256:{'b' * 64}"),
            "workflow_id": WORKFLOW_ID,
            "workflow_version": WorkflowVersion(WORKFLOW_ID, 4),
            "fencing_token": 8,
            "event_type": LeaseEventType.ATOMIC_CALL_STARTED,
            "occurred_at": NOW,
            "state_before": LeaseState.HELD_EXECUTION,
            "state_after": LeaseState.HELD_EXECUTION,
            "reason": "persisted atomic-call intent",
            "evidence_reference": "evidence:v1:atomic-intent",
            "command_id": COMMAND_ID,
            "operation_id": OPERATION_ID,
            "correlation_id": CORRELATION_ID,
        }
        values.update(overrides)
        return LeaseEvent(**values)  # type: ignore[arg-type]

    def test_lease_and_events_are_immutable(self) -> None:
        for record in (self.lease(), self.event()):
            with self.subTest(record=type(record).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(record, "fencing_token", 9)

    def test_context_lease_binds_workflow_scope_and_lifecycle(self) -> None:
        self.assertEqual(LeaseState.HELD_EXECUTION, self.lease().state)
        with self.assertRaises(ValueError):
            self.lease(workflow_version=WorkflowVersion(OWNER_ID, 4))
        with self.assertRaises(ValueError):
            self.lease(state=LeaseState.HELD_PREVIEW)
        with self.assertRaises(ValueError):
            self.lease(state=LeaseState.AVAILABLE)
        with self.assertRaises(ValueError):
            self.lease(expires_at=NOW)
        with self.assertRaises(ValueError):
            self.lease(owner_instance_id="owner")

    def test_context_lease_requires_valid_token_and_recovery_bindings(self) -> None:
        with self.assertRaises(ValueError):
            self.lease(fencing_token=0)
        with self.assertRaises(TypeError):
            self.lease(fencing_token=True)
        with self.assertRaises(ValueError):
            self.lease(state=LeaseState.EXPIRED)
        with self.assertRaises(ValueError):
            self.lease(
                state=LeaseState.EXPIRED,
                operation_id=OPERATION_ID,
                recovery_disposition="expired before call",
            )
        with self.assertRaises(ValueError):
            self.lease(state=LeaseState.IN_FLIGHT_EXPIRED, recovery_disposition="outcome unknown")
        self.assertEqual(
            LeaseState.IN_FLIGHT_EXPIRED,
            self.lease(
                state=LeaseState.IN_FLIGHT_EXPIRED,
                operation_id=OPERATION_ID,
                recovery_disposition="native outcome requires reconciliation",
            ).state,
        )
        with self.assertRaises(ValueError):
            self.lease(recovery_disposition="must not be present while held")

    def test_lease_event_binds_scope_and_atomic_operation_evidence(self) -> None:
        self.assertEqual(8, self.event().fencing_token)
        with self.assertRaises(ValueError):
            self.event(workflow_version=WorkflowVersion(OWNER_ID, 4))
        with self.assertRaises(ValueError):
            self.event(event_type=LeaseEventType.ATOMIC_CALL_STARTED, operation_id=None)
        with self.assertRaises(ValueError):
            self.event(reason="x" * 1025)
        with self.assertRaises(ValueError):
            self.event(correlation_id="not-a-uuid")
        with self.assertRaises(ValueError):
            self.event(occurred_at=datetime(2026, 7, 15, 12, 0))


if __name__ == "__main__":
    unittest.main()
