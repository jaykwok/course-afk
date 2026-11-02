from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class Status(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_CHILDREN = "WAITING_CHILDREN"
    WAITING_SYNC = "WAITING_SYNC"
    BLOCKED = "BLOCKED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


@dataclass
class TaskOutcome:
    task_id: str
    status: str
    reason_codes: list[str] = field(default_factory=list)
    child_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    retry_policy: str = "manual"
    stop_scope: str = "task"
    details: dict = field(default_factory=dict)
    schema_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)
