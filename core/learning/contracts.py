"""Separate local learning completion from outstanding child tasks."""

from dataclasses import dataclass


@dataclass(frozen=True)
class LearningResult:
    completed: bool
    local_work_complete: bool
    evidence_refs: tuple[str, ...] = ()

    def __bool__(self):
        return self.completed

    @classmethod
    def from_value(cls, value):
        if isinstance(value, cls):
            return value
        completed = value is True
        return cls(
            completed,
            completed,
            ("server_section_progress",) if completed else (),
        )
