from __future__ import annotations

import re

from core.file_ops import is_compliant_url_regex, is_exam_url, normalize_url


URL_PATTERN = re.compile(r"https?://[^\s<>'\"，,；;]+", re.IGNORECASE)
LEARNING_ZONE_PATTERN = re.compile(r"/topic(?:/|[?#])", re.IGNORECASE)


def unique_urls(urls: list[str]) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            results.append(url)
            seen.add(url)
    return results


def extract_urls_from_text(text: str) -> list[str]:
    matches = [match.strip() for match in URL_PATTERN.findall(text or "")]
    return unique_urls(matches)


def normalize_urls(urls: list[str]) -> list[str]:
    return unique_urls([normalize_url(url.strip()) for url in urls if url.strip()])


def is_learning_zone_url(url: str) -> bool:
    return bool(LEARNING_ZONE_PATTERN.search((url or "").strip()))


def split_manual_selection_urls(
    urls: list[str],
) -> tuple[list[str], list[str], list[str], list[str]]:
    learning_urls: list[str] = []
    exam_urls: list[str] = []
    learning_zone_urls: list[str] = []
    entry_urls: list[str] = []
    for url in normalize_urls(urls):
        if is_compliant_url_regex(url):
            learning_urls.append(url)
        elif is_exam_url(url):
            exam_urls.append(url)
        elif is_learning_zone_url(url):
            learning_zone_urls.append(url)
        else:
            entry_urls.append(url)
    return learning_urls, exam_urls, learning_zone_urls, entry_urls
