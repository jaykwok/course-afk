from dataclasses import dataclass, field


@dataclass(frozen=True)
class CollectionResult:
    added: int = 0
    complete: bool = False
    reason_codes: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoveryManifest:
    """One entry's children and evidence; readers never recursively dispatch."""
    links: tuple[str, ...] = ()
    complete: bool = False
    source: str = "entry_links"
    message: str = ""
    residual_subject_url: str | None = None
    details: dict = field(default_factory=dict)
