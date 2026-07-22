from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from core.browser.session import create_browser_context
from core.config import ZHIXUEYUN_COURSE_PREFIX, ZHIXUEYUN_SUBJECT_PREFIX
from core.file_ops import is_compliant_url_regex, normalize_url
from core.links import unique_urls
from core.browser.overlays import (
    dismiss_topmost_overlays_async,
    prepare_page_after_navigation_async,
)
from core.discovery.subject_parse import enqueue_learning_links_with_subject_expand


# 实勘（cms topic / 学习专区）:
# - 非知学云 class API；链接主要在静态/半静态 HTML 中
# - 需点「更多 / 查看更多」展开后才能收全
# - 弹窗先关再读 DOM（通用 dismiss，不绑文案）
LEARNING_ZONE_LINK_WAIT_MILLISECONDS = 15000
LEARNING_ZONE_LINK_POLL_MILLISECONDS = 500
_MORE_CLICK_MAX = 20
_MORE_CLICK_WAIT_MS = 1200
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_DETAIL_IN_TEXT = re.compile(
    rf"(?:https?://kc\.zhixueyun\.com)?/?#?/study/(course|subject)/detail/({_UUID})",
    re.IGNORECASE,
)
_APP_RESOURCE = re.compile(
    rf"businessType=([12]).{{0,80}}?businessId=({_UUID})"
    rf"|businessId=({_UUID}).{{0,80}}?businessType=([12])",
    re.IGNORECASE | re.DOTALL,
)

_CLICK_MORE_SCRIPT = """
() => {
  // 精确匹配，避免「更多」误点到其它含字控件
  const exact = new Set(['查看更多', '加载更多', '更多', '展开更多', '显示更多']);
  const prefixOk = ['查看更多', '加载更多', '展开更多', '显示更多'];
  for (const el of document.querySelectorAll('a,button,span,div,li')) {
    const t = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
    if (!t || t.length > 16) continue;
    const hit = exact.has(t) || prefixOk.some(l => t.startsWith(l) && t.length <= l.length + 4);
    if (!hit) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    el.click();
    return { ok: true, text: t.slice(0, 24) };
  }
  return { ok: false };
}
"""


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

    if "/app/" in href or "businessType=" in href:
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
    """
    从 topic / 学习专区 HTML 提取课程与主题链接。

    实勘: 不仅 <a href>，正文/脚本里也有 #/study/.../detail/UUID。
    """
    text = html_content or ""
    links: list[str] = []

    soup = BeautifulSoup(text, "html.parser")
    for link in soup.find_all("a"):
        normalized = _normalize_learning_zone_href(link.get("href"))
        if normalized:
            links.append(normalized)
        # 部分卡片把地址写在其它属性
        for attr in ("data-href", "data-url", "data-link", "onclick"):
            raw = link.get(attr)
            if raw:
                links.extend(_extract_detail_urls_from_text(str(raw)))

    links.extend(_extract_detail_urls_from_text(text))
    return unique_urls(links)


def _extract_detail_urls_from_text(text: str) -> list[str]:
    results: list[str] = []
    for match in _DETAIL_IN_TEXT.finditer(text or ""):
        kind = match.group(1).lower()
        uuid = match.group(2)
        prefix = (
            ZHIXUEYUN_COURSE_PREFIX if kind == "course" else ZHIXUEYUN_SUBJECT_PREFIX
        )
        candidate = f"{prefix}{uuid}"
        if is_compliant_url_regex(candidate):
            results.append(candidate)

    for match in _APP_RESOURCE.finditer(text or ""):
        if match.group(1) and match.group(2):
            business_type, business_id = match.group(1), match.group(2)
        else:
            business_id, business_type = match.group(3), match.group(4)
        if business_type == "1":
            candidate = f"{ZHIXUEYUN_COURSE_PREFIX}{business_id}"
        elif business_type == "2":
            candidate = f"{ZHIXUEYUN_SUBJECT_PREFIX}{business_id}"
        else:
            continue
        if is_compliant_url_regex(candidate):
            results.append(candidate)
    return results


async def _click_load_more_until_stable(page, status_callback=None) -> int:
    """点击「更多」类按钮直到不再出现新按钮、链接不再增加，或达到上限。"""
    clicks = 0
    prev_count = len(
        extract_learning_links_from_learning_zone_html(await page.content())
    )
    for _ in range(_MORE_CLICK_MAX):
        await dismiss_topmost_overlays_async(page, max_count=2)
        try:
            result = await page.evaluate(_CLICK_MORE_SCRIPT)
        except Exception:
            break
        if not result or not result.get("ok"):
            break
        clicks += 1
        if status_callback and clicks == 1:
            status_callback("正在展开学习专区「更多」内容…")
        await page.wait_for_timeout(_MORE_CLICK_WAIT_MS)
        new_count = len(
            extract_learning_links_from_learning_zone_html(await page.content())
        )
        # 点了但链接数不涨，且连续无效则停（避免误点空转）
        if new_count <= prev_count:
            # 再给一页懒加载机会；仍不涨则结束
            await page.wait_for_timeout(_MORE_CLICK_WAIT_MS)
            new_count = len(
                extract_learning_links_from_learning_zone_html(await page.content())
            )
            if new_count <= prev_count:
                break
        prev_count = new_count
    return clicks


async def collect_learning_links_from_learning_zone_urls(
    learning_zone_urls: list[str],
    status_callback=None,
    before_close_callback=None,
    *,
    context=None,
) -> int:
    """
    解析学习专区链接并写入学习队列。

    可传入已有 context 复用浏览器；否则自建 context。
    """
    if not learning_zone_urls:
        return 0

    total_added = 0

    async def _run_with_context(active_context) -> None:
        nonlocal total_added
        for index, url in enumerate(learning_zone_urls, start=1):
            if status_callback:
                status_callback(
                    f"正在解析学习专区链接 {index}/{len(learning_zone_urls)}"
                )
            page = await active_context.new_page()
            try:
                await page.goto(url, wait_until="load")
                await prepare_page_after_navigation_async(
                    page, status_callback=status_callback
                )

                elapsed = 0
                learning_links: list[str] = []
                # 先等首屏出现任意合规链接，再点更多收全
                while elapsed <= LEARNING_ZONE_LINK_WAIT_MILLISECONDS:
                    dismissed_count = await dismiss_topmost_overlays_async(page)
                    if dismissed_count and status_callback:
                        status_callback(
                            f"已关闭 {dismissed_count} 个页面弹窗，继续读取课程链接"
                        )
                    learning_links = extract_learning_links_from_learning_zone_html(
                        await page.content()
                    )
                    if learning_links:
                        break
                    if elapsed >= LEARNING_ZONE_LINK_WAIT_MILLISECONDS:
                        break
                    await page.wait_for_timeout(LEARNING_ZONE_LINK_POLL_MILLISECONDS)
                    elapsed += LEARNING_ZONE_LINK_POLL_MILLISECONDS

                more_clicks = await _click_load_more_until_stable(
                    page, status_callback=status_callback
                )
                if more_clicks and status_callback:
                    status_callback(f"已展开「更多」{more_clicks} 次")

                learning_links = extract_learning_links_from_learning_zone_html(
                    await page.content()
                )
                # 再从渲染后的 DOM 文本兜底扫一轮（含非 a 标签）
                try:
                    body_text = await page.evaluate(
                        "() => (document.body && document.body.innerText) || ''"
                    )
                    learning_links = unique_urls(
                        learning_links + _extract_detail_urls_from_text(body_text or "")
                    )
                except Exception:
                    pass

                enqueue = await enqueue_learning_links_with_subject_expand(
                    learning_links,
                    page=page,
                    status_callback=status_callback,
                    source_label="学习专区",
                )
                total_added += enqueue["learning_added"]
                if status_callback:
                    if learning_links:
                        status_callback(
                            "已从学习专区识别 "
                            f"{enqueue['course_links']} 条课程、"
                            f"{enqueue['subject_links']} 个主题"
                            f"（展开后学习队列 +{enqueue['learning_added']}）"
                        )
                    else:
                        status_callback(
                            "该学习专区暂未识别到课程链接；已处理弹窗并等待页面加载，"
                            "可改用手动选择模式"
                        )
            finally:
                await page.close()

    if context is not None:
        await _run_with_context(context)
        if before_close_callback:
            before_close_callback(total_added)
    else:
        async with create_browser_context() as (_, new_context):
            await _run_with_context(new_context)
            if before_close_callback:
                before_close_callback(total_added)

    return total_added
