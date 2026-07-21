from __future__ import annotations

import re

from core.file_ops import (
    is_compliant_url_regex,
    is_exam_url,
    is_train_class_url,
    normalize_url,
)


URL_PATTERN = re.compile(r"https?://[^\s<>'\"，,；;]+", re.IGNORECASE)
LEARNING_ZONE_PATTERN = re.compile(r"/topic(?:/|[?#])", re.IGNORECASE)


def unique_urls(urls: list[str] | None) -> list[str]:
    """保序去重，跳过空串。全项目 URL 列表去重请用此函数。"""
    results: list[str] = []
    seen: set[str] = set()
    for raw in urls or []:
        url = (raw or "").strip() if isinstance(raw, str) else raw
        if not url or url in seen:
            continue
        seen.add(url)
        results.append(url)
    return results


def extract_urls_from_text(text: str) -> list[str]:
    matches = [match.strip() for match in URL_PATTERN.findall(text or "")]
    return unique_urls(matches)


def normalize_urls(urls: list[str] | None) -> list[str]:
    """normalize_url 后保序去重（过滤空串）。"""
    return unique_urls(
        [normalize_url((url or "").strip()) for url in (urls or []) if (url or "").strip()]
    )


def is_learning_zone_url(url: str) -> bool:
    return bool(LEARNING_ZONE_PATTERN.search((url or "").strip()))


def split_manual_selection_urls(
    urls: list[str],
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    learning_urls: list[str] = []
    exam_urls: list[str] = []
    learning_zone_urls: list[str] = []
    train_class_urls: list[str] = []
    entry_urls: list[str] = []
    for url in normalize_urls(urls):
        if is_compliant_url_regex(url):
            learning_urls.append(url)
        elif is_exam_url(url):
            exam_urls.append(url)
        elif is_learning_zone_url(url):
            learning_zone_urls.append(url)
        elif is_train_class_url(url):
            train_class_urls.append(url)
        else:
            entry_urls.append(url)
    return learning_urls, exam_urls, learning_zone_urls, train_class_urls, entry_urls
