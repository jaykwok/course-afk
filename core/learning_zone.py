from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from core.browser import create_browser_context
from core.config import ZHIXUEYUN_COURSE_PREFIX, ZHIXUEYUN_SUBJECT_PREFIX
from core.file_ops import is_compliant_url_regex, normalize_url
from core.learning_queue import append_learning_urls


def _unique_urls(urls: list[str]) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url and url not in seen:
            results.append(url)
            seen.add(url)
    return results


def _extract_query_params_from_app_href(href: str) -> dict[str, list[str]]:
    parsed_url = urlparse(href.strip())
    params = parse_qs(parsed_url.query)
    if params:
        return params

    fragment = parsed_url.fragment.lstrip("/")
    fragment_parts = fragment.split("?", 1)
    if len(fragment_parts) > 1:
        return parse_qs(fragment_parts[1])
    return {}


def _normalize_learning_zone_href(href: str) -> str | None:
    href = (href or "").strip()
    if not href or "kc.zhixueyun.com" not in href:
        return None

    if "/app/" in href:
        params = _extract_query_params_from_app_href(href)
        business_id = params.get("businessId", [None])[0]
        business_type = params.get("businessType", [None])[0]
        if business_type == "1" and business_id:
            return f"{ZHIXUEYUN_COURSE_PREFIX}{business_id}"
        if business_type == "2" and business_id:
            return f"{ZHIXUEYUN_SUBJECT_PREFIX}{business_id}"

    normalized = normalize_url(href)
    if is_compliant_url_regex(normalized):
        return normalized
    return None


def extract_learning_links_from_learning_zone_html(html_content: str) -> list[str]:
    soup = BeautifulSoup(html_content or "", "html.parser")
    links: list[str] = []
    for link in soup.find_all("a"):
        normalized = _normalize_learning_zone_href(link.get("href"))
        if normalized:
            links.append(normalized)
    return _unique_urls(links)


async def collect_learning_links_from_learning_zone_urls(
    learning_zone_urls: list[str],
    status_callback=None,
) -> int:
    if not learning_zone_urls:
        return 0

    total_added = 0
    async with create_browser_context() as (_, context):
        for index, url in enumerate(learning_zone_urls, start=1):
            if status_callback:
                status_callback(
                    f"正在解析学习专区链接 {index}/{len(learning_zone_urls)}"
                )
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="load")
                await page.wait_for_timeout(1500)
                learning_links = extract_learning_links_from_learning_zone_html(
                    await page.content()
                )
                added = append_learning_urls(learning_links)
                total_added += len(added)
                if status_callback:
                    status_callback(
                        f"已从学习专区链接新增 {len(added)} 条学习链接"
                    )
            finally:
                await page.close()

    return total_added
