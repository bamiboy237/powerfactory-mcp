"""Vendor-neutral execution and evidence handling for lifecycle probes."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


class LifecycleStage(str, Enum):
    ENVIRONMENT = "environment"
    IMPORT_MODULE = "import_module"
    CONNECT_APPLICATION = "connect_application"
    ACTIVATE_PROJECT = "activate_project"
    ACTIVATE_STUDY_CASE = "activate_study_case"
    INVENTORY = "inventory"
    LOAD_FLOW = "load_flow"
    RESULTS = "results"
    CAPABILITIES = "capabilities"
    IDENTITY = "identity"
    CLEANUP = "cleanup"


class StageStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"


class IdentityOperation(str, Enum):
    DUPLICATE_NAME = "duplicate_name"
    RENAME = "rename"
    MOVE = "move"
    COPY = "copy"
    DELETE = "delete"
    RECREATE_SAME_NAME = "recreate_same_name"


@dataclass(frozen=True)
class IdentityObservation:
    """Raw candidate-identity evidence, without declaring an identity policy."""

    operation: IdentityOperation
    subject: str
    before_identity: str | None
    after_identity: str | None
    related_identity: str | None = None
    before_locator: str | None = None
    after_locator: str | None = None
    exists_after: bool = True
    note: str = ""


@runtime_checkable
class LifecycleAdapter(Protocol):
    """Boundary implemented by a fake or a Windows PowerFactory adapter."""

    def execute_stage(self, stage: LifecycleStage) -> Mapping[str, Any]:
        """Execute one named stage and return evidence safe for sanitization."""


@dataclass(frozen=True)
class StageEvidence:
    stage: LifecycleStage
    status: StageStatus
    data: Mapping[str, Any]
    error_type: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return sanitize_evidence(asdict(self))


@dataclass(frozen=True)
class RunEvidence:
    run: int
    status: StageStatus
    failure_stage: LifecycleStage | None
    stages: tuple[StageEvidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return sanitize_evidence(asdict(self))


@dataclass(frozen=True)
class ProbeEvidence:
    schema_version: int
    runs: tuple[RunEvidence, ...]

    @property
    def passed(self) -> bool:
        return all(run.status is StageStatus.PASS for run in self.runs)

    def to_dict(self) -> dict[str, Any]:
        return sanitize_evidence(asdict(self))


_EXECUTION_STAGES = tuple(stage for stage in LifecycleStage if stage is not LifecycleStage.CLEANUP)
_SENSITIVE_KEY_PARTS = (
    "credential",
    "licence_key",
    "license_key",
    "password",
    "secret",
    "token",
)
_REDACTED = "[REDACTED]"
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(password|token|secret|credential|licen[cs]e_key)\s*([:=])\s*(\S+)"
)


def sanitize_evidence(value: Any) -> Any:
    """Convert evidence to deterministic JSON values and redact secret fields."""

    if is_dataclass(value) and not isinstance(value, type):
        return sanitize_evidence(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("evidence mapping keys must be strings")
        sanitized: dict[str, Any] = {}
        for key in sorted(value):
            normalized_key = key.casefold().replace("-", "_")
            if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
                sanitized[key] = _REDACTED
            else:
                sanitized[key] = sanitize_evidence(value[key])
        return sanitized
    if isinstance(value, (set, frozenset)):
        sanitized_items = [sanitize_evidence(item) for item in value]
        return sorted(sanitized_items, key=_json_sort_key)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize_evidence(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, str):
        return _sanitize_error_message(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError(f"unsupported evidence value: {type(value).__name__}")


def _json_sort_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False)


def _sanitize_error_message(message: str) -> str:
    return _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}", message
    )


class LifecycleProbeRunner:
    """Run the full lifecycle repeatedly while isolating each run's failure."""

    def __init__(self, adapter: LifecycleAdapter) -> None:
        self._adapter = adapter

    def run(self, repeat: int = 1) -> ProbeEvidence:
        if repeat < 1:
            raise ValueError("repeat must be at least 1")

        runs = tuple(self._run_once(run_number) for run_number in range(1, repeat + 1))
        return ProbeEvidence(schema_version=1, runs=runs)

    def _run_once(self, run_number: int) -> RunEvidence:
        records: list[StageEvidence] = []
        failure_stage: LifecycleStage | None = None

        for stage in _EXECUTION_STAGES:
            if failure_stage is not None:
                records.append(StageEvidence(stage, StageStatus.SKIPPED, {}))
                continue
            record = self._execute(stage)
            records.append(record)
            if record.status is StageStatus.FAIL:
                failure_stage = stage

        cleanup_record = self._execute(LifecycleStage.CLEANUP)
        records.append(cleanup_record)
        if cleanup_record.status is StageStatus.FAIL and failure_stage is None:
            failure_stage = LifecycleStage.CLEANUP

        status = StageStatus.FAIL if failure_stage is not None else StageStatus.PASS
        return RunEvidence(run_number, status, failure_stage, tuple(records))

    def _execute(self, stage: LifecycleStage) -> StageEvidence:
        try:
            data = self._adapter.execute_stage(stage)
            if not isinstance(data, Mapping):
                raise TypeError("adapter stage evidence must be a mapping")
            return StageEvidence(stage, StageStatus.PASS, sanitize_evidence(data))
        except Exception as exc:  # Probe evidence must retain the failed stage and continue.
            return StageEvidence(
                stage=stage,
                status=StageStatus.FAIL,
                data={},
                error_type=type(exc).__name__,
                error_message=_sanitize_error_message(str(exc)),
            )


def write_evidence_json(evidence: ProbeEvidence, output: str | Path) -> None:
    """Write stable, newline-terminated JSON suitable for hashing and diffing."""

    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(
        evidence.to_dict(),
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    )
    path.write_text(f"{rendered}\n", encoding="utf-8", newline="\n")
