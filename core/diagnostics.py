"""Shared redaction for logs and structural diagnostic exports."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE = re.compile(r"authorization|cookie|password|passwd|secret|bootstrap|api.?key|token|auth.?key|signature|^sign$|access.?key|credential|session|^policy$|key-pair-id", re.I)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        query = urlencode([(key, "[redacted]" if _SENSITIVE.search(key) or key.lower() in {"code", "ticket"} else val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)])
        fragment = parsed.fragment
        if re.match(r"^/?login/", fragment, re.I):
            fragment = re.sub(r"^(/?)login/.*", r"\1login/[redacted]", fragment, flags=re.I)
        if "?" in fragment:
            route, params = fragment.split("?", 1)
            fragment = route + "?" + urlencode([(key, "[redacted]" if _SENSITIVE.search(key) or key.lower() in {"code", "ticket"} else val) for key, val in parse_qsl(params, keep_blank_values=True)])
        elif "=" in fragment and not fragment.startswith("/"):
            fragment = urlencode([(key, "[redacted]" if _SENSITIVE.search(key) else val)
                                  for key, val in parse_qsl(fragment, keep_blank_values=True)])
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))
    except ValueError:
        return "[invalid URL]"


def redact_text(value: object) -> str:
    text = _URL.sub(lambda match: redact_url(match.group()), str(value or ""))
    text = re.sub(r"\bBearer(?:__|\s+)[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text, flags=re.I)
    text = re.sub(r"(?i)([\"']?(?:authorization|cookie|password|api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?key|token|secret)[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)", r"\1[redacted]", text)
    return text


def redact(value):
    if isinstance(value, dict):
        return {key: "[redacted]" if _SENSITIVE.search(str(key)) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return redact_text(value) if isinstance(value, str) else value


def structural_html(html: str) -> str:
    """Preserve tag/class layout, remove all user text and payload-bearing attrs."""
    if "<" not in html:
        return "[text]" if html.strip() else ""
    from bs4 import BeautifulSoup, Comment
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style"]):
        element.decompose()
    for text in list(soup.find_all(string=True)):
        text.replace_with("" if isinstance(text, Comment) or not str(text).strip() else "[text]")
    safe_attributes = {"class", "role", "type", "data-sectiontype", "aria-checked", "aria-disabled"}
    for element in soup.find_all(True):
        element.attrs = {key: value for key, value in element.attrs.items() if key in safe_attributes}
    return str(soup)


def redact_snapshot(value):
    """Diagnostic layout/shape is useful without storing the user's full content."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if "html" in str(key).lower() and isinstance(item, str):
                result[key] = structural_html(item)
            elif str(key).lower() in {"bodytext", "body_text", "text", "texts", "title", "question", "answer"}:
                result[key] = "[content omitted]"
            elif _SENSITIVE.search(str(key)) and not isinstance(item, (bool, int)):
                result[key] = "[redacted]"
            else:
                result[key] = redact_snapshot(item)
        return result
    if isinstance(value, list):
        return [redact_snapshot(item) for item in value]
    return redact_text(value) if isinstance(value, str) else value
