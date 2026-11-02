from dataclasses import dataclass, field


@dataclass(frozen=True)
class CollectionResult:
    added: int = 0
    complete: bool = False
    reason_codes: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)
