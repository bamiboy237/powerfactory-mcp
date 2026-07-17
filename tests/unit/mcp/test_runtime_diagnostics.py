from __future__ import annotations

import json
from pathlib import Path
import tempfile

from powerfactory_agent.gateway import OperationResultUnavailableError
from powerfactory_agent.mcp.configuration import create_installation
from powerfactory_agent.mcp.runtime import PowerFactoryEngineeringRuntime
from powerfactory_agent.persistence import OperationRecord, OperationState


class RecordingOwner:
    def diagnostics(self) -> dict[str, bool | str | None]:
        return {
            "quarantined": True,
            "stopping": False,
            "active_operation_id": "50000000-0000-4000-8000-000000000001",
            "owner_thread_alive": True,
            "watchdog_thread_alive": True,
        }


def test_runtime_diagnostic_persists_only_sanitized_operation_and_owner_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        installation = create_installation(Path(directory) / "agent")
        runtime = object.__new__(PowerFactoryEngineeringRuntime)
        runtime._installation = installation
        runtime._owner = RecordingOwner()

        diagnostic = runtime._persist_runtime_diagnostic(
            {
                "operation_id": "50000000-0000-4000-8000-000000000001",
                "handler": "powerfactory.gateway.v1.query-objects",
                "state": "RECONCILIATION_REQUIRED",
                "exception_category": "uncertain_handler_outcome",
                "exception_type": "NativePowerFactoryError",
            }
        )

        path = installation.log_file.parent / "evidence" / diagnostic["evidence_id"]
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted == diagnostic
        assert persisted["operation"]["handler"] == "powerfactory.gateway.v1.query-objects"
        assert persisted["operation"]["exception_type"] == "NativePowerFactoryError"
        assert persisted["owner"]["quarantined"] is True
        assert "message" not in persisted["operation"]


def test_unavailable_operation_diagnostic_drops_raw_native_message() -> None:
    with tempfile.TemporaryDirectory() as directory:
        installation = create_installation(Path(directory) / "agent")
        runtime = object.__new__(PowerFactoryEngineeringRuntime)
        runtime._installation = installation
        runtime._owner = RecordingOwner()
        record = OperationRecord(
            sequence=1,
            operation_id="50000000-0000-4000-8000-000000000001",
            idempotency_key="test-operation",
            handler_name="powerfactory.gateway.v1.query-objects",
            payload={},
            state=OperationState.RECONCILIATION_REQUIRED,
            admitted_at_ns=1,
            queue_deadline_at_ns=2,
            client_deadline_at_ns=3,
            engine_health_threshold_ms=100,
            started_at_ns=1,
            finished_at_ns=2,
            result=None,
            error={
                "category": "uncertain_handler_outcome",
                "exception_type": "NativePowerFactoryError",
                "message": "credential=do-not-persist",
            },
            version=1,
        )

        diagnostic = runtime._persist_operation_failure(OperationResultUnavailableError(record))

        assert diagnostic["operation"] == {
            "operation_id": record.operation_id,
            "handler": record.handler_name,
            "state": "RECONCILIATION_REQUIRED",
            "exception_category": "uncertain_handler_outcome",
            "exception_type": "NativePowerFactoryError",
        }
        persisted = (installation.log_file.parent / "evidence" / diagnostic["evidence_id"]).read_text(
            encoding="utf-8"
        )
        assert "credential=do-not-persist" not in persisted
