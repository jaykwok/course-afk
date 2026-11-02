"""One identity parser for navigation aliases, queues and history."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import html
import json
import re
from urllib.parse import parse_qs, urlsplit


def account_id() -> str:
    from core.auth.credential import (
        load_credential_metadata,
        load_pending_owner_metadata,
    )

    metadata = load_credential_metadata() or load_pending_owner_metadata()
    # Display labels and cookie bytes are deliberately not identities.
    name = metadata.account_name.strip() if metadata else ""
    return hashlib.sha256(name.encode()).hexdigest()[:16] if name else "unbound"


def canonical_url(url: str) -> str:
    from core.file_ops import normalize_url

    value = normalize_url(html.unescape(url))
    return re.sub(r"/exam/(?:exam-detail|answer-paper)/", "/exam/answer-paper/", value)


@dataclass(frozen=True)
class TaskKey:
    tenant: str
    account: str
    kind: str
    business_id: str
    source_type: str | None = None
    source_id: str | None = None
    required: str | None = None

    @property
    def task_id(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()


@dataclass(frozen=True)
class Entry:
    key: TaskKey
    url: str
    aliases: tuple[str, ...]


def parse_entry(url: str, account: str, *, kind: str | None = None) -> Entry:
    original = html.unescape(url.strip())
    value = canonical_url(original)
    parsed = urlsplit(value)
    route = parsed.fragment.split("?", 1)[0]
    tenant = (parsed.hostname or "unknown").lower()
    source_type = source_id = required = None
    patterns = (
        ("course", r"/study/course/detail/([^/]+)(?:/(\d+)/([01])/([^/]+))?$"),
        ("subject", r"/study/subject/detail/([^/]+)$"),
        ("exam", r"/exam/answer-paper/([^/]+)$"),
        ("class", r"/train-new/class-detail/([^/]+)$"),
    )
    business_id = hashlib.sha256(value.encode()).hexdigest()
    found_kind = "entry"
    for candidate, pattern in patterns:
        match = re.search(pattern, route)
        if match:
            found_kind, business_id = candidate, match.group(1).lower()
            if candidate == "course":
                source_type, required, source_id = match.groups()[1:]
                source_id = source_id.lower() if source_id else None
            break
    organization = parse_qs(parsed.query).get("organization", [None])[0]
    # Organization aliases are only merged after a verified route resolves them.
    if organization and found_kind == "entry":
        tenant += ":" + organization
    return Entry(
        TaskKey(
            tenant,
            account,
            kind or found_kind,
            business_id,
            source_type,
            source_id,
            required,
        ),
        value,
        tuple(dict.fromkeys((original, value))),
    )
