from __future__ import annotations

import logging
import re
from urllib.parse import unquote

from core.browser import create_browser_context
from core.config import ZHIXUEYUN_COURSE_PREFIX, ZHIXUEYUN_SUBJECT_PREFIX
from core.file_ops import is_compliant_url_regex, is_train_class_url, normalize_url
from core.links import unique_urls
from core.page_auth import fetch_json, wait_for_authorization_header
from core.page_overlays import prepare_page_after_navigation_async
from core.subject_parse import enqueue_learning_links_with_subject_expand


# 实勘确认（class 活动列表）:
# - 鉴权: Authorization: Bearer__{token}
# - 章节: GET .../chapter/paas?classId=
# - 首屏: GET .../chapter-activity-list/paas?classId=&chapterId=&page=1&pageSize=5
# - 查看更多: GET .../chapter-activity-list/paas/more?...&page=2|3|...&pageSize=5
# - 首页 recordCount 不可信（本班报 5，实际 13）；以「本批条数 < pageSize」结束分页
_API_HOST = "https://kc.zhixueyun.com"
_CHAPTER_API = (
    f"{_API_HOST}/api/v1/training/student/class-info/"
    "safe/chapter/paas?classId={class_id}"
)
_ACTIVITY_LIST_API = (
    f"{_API_HOST}/api/v1/training/student/class-info/"
    "safe/chapter-activity-list/paas"
    "?classId={class_id}&chapterId={chapter_id}&page={page}&pageSize={page_size}"
)
_ACTIVITY_MORE_API = (
    f"{_API_HOST}/api/v1/training/student/class-info/"
    "safe/chapter-activity-list/paas/more"
    "?classId={class_id}&chapterId={chapter_id}&page={page}&pageSize={page_size}"
)
# UI「查看更多」实勘 pageSize=5；末页可少于 5（如 +3）。
_ACTIVITY_PAGE_SIZE = 5
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_CLASS_ID_FROM_DETAIL = re.compile(
    rf"/train-new/class-detail/({_UUID})",
    re.IGNORECASE,
)
_CLASS_ID_FROM_QUERY = re.compile(
    rf"classId=({_UUID})",
    re.IGNORECASE,
)
# 活动 businessType → 可挂机前缀（实勘 UI 标签 + 现有 qrScan 规则）。
# 1=course, 2=subject, 8=课程（UI 前缀「课程」，businessId 为 UUID）
_DIRECT_ACTIVITY_TYPE_PREFIX = {
    "1": ZHIXUEYUN_COURSE_PREFIX,
    "2": ZHIXUEYUN_SUBJECT_PREFIX,
    "8": ZHIXUEYUN_COURSE_PREFIX,
}


def extract_class_id(url: str) -> str | None:
    """从培训班标准链接或 paas classId 参数中提取班级 UUID。"""
    if not url:
        return None
    normalized = normalize_url(url)
    for candidate in (normalized, url, unquote(url)):
        match = _CLASS_ID_FROM_DETAIL.search(candidate)
        if match:
            return match.group(1)
        match = _CLASS_ID_FROM_QUERY.search(candidate)
        if match:
            return match.group(1)
    return None


def build_activity_list_url(
    *,
    class_id: str,
    chapter_id: str,
    page: int,
    page_size: int = _ACTIVITY_PAGE_SIZE,
) -> str:
    """
    构造活动列表 URL。

    page=1 用首屏接口；page>=2 用「查看更多」对应的 /more 接口。
    """
    template = _ACTIVITY_LIST_API if page <= 1 else _ACTIVITY_MORE_API
    return template.format(
        class_id=class_id,
        chapter_id=chapter_id,
        page=page,
        page_size=page_size,
    )


def map_activity_to_learning_url(activity: dict) -> str | None:
    """
    将班级活动项映射为可挂机的课程/主题链接。

    实勘规则:
    - businessValue 含可归一化 URL 时优先（如 type=11 外链里嵌套 businessType=2 主题）
    - type 1 / 8 + businessId → course/detail/{uuid}
    - type 2 + businessId → subject/detail/{uuid}
    - 其它类型且无可用 businessValue 时跳过
    """
    if not isinstance(activity, dict):
        return None

    business_value = str(activity.get("businessValue") or "").strip()
    if business_value:
        normalized = normalize_url(unquote(business_value))
        if is_compliant_url_regex(normalized):
            return normalized

    type_code = str(activity.get("businessType") or "").strip()
    business_id = str(activity.get("businessId") or "").strip()
    prefix = _DIRECT_ACTIVITY_TYPE_PREFIX.get(type_code)
    if prefix and re.fullmatch(_UUID, business_id, re.IGNORECASE):
        candidate = f"{prefix}{business_id}"
        if is_compliant_url_regex(candidate):
            return candidate
    return None


def extract_learning_links_from_activity_items(items: list) -> list[str]:
    """从活动列表 items 提取课程/主题链接（去重保序）。"""
    results: list[str] = []
    for item in items or []:
        mapped = map_activity_to_learning_url(item)
        if mapped:
            results.append(mapped)
    return unique_urls(results)


def extract_learning_links_from_train_class_content(content: str) -> list[str]:
    """
    从接口 JSON 文本中提取可挂机链接（secondary 兜底；主路径用结构化 chapter/activity API）。

    只认 JSON 字段形态的 businessValue / businessType+businessId。
    """
    text = content or ""
    results: list[str] = []
    decoded = unquote(text)
    # 直接 type：1 / 2 / 8
    type_group = "1|2|8"

    for candidate in (text, decoded):
        for match in re.finditer(
            rf"({re.escape(ZHIXUEYUN_COURSE_PREFIX)}|{re.escape(ZHIXUEYUN_SUBJECT_PREFIX)})"
            rf"({_UUID})",
            candidate,
            re.IGNORECASE,
        ):
            results.append(f"{match.group(1)}{match.group(2)}")

        for match in re.finditer(
            r"[\"']businessValue[\"']\s*:\s*[\"']([^\"']+)[\"']",
            candidate,
            re.IGNORECASE,
        ):
            mapped = map_activity_to_learning_url({"businessValue": match.group(1)})
            if mapped:
                results.append(mapped)

        for match in re.finditer(
            rf"[\"']businessType[\"']\s*:\s*[\"']?({type_group})[\"']?"
            rf".{{0,80}}?"
            rf"[\"']businessId[\"']\s*:\s*[\"']({_UUID})[\"']",
            candidate,
            re.IGNORECASE | re.DOTALL,
        ):
            mapped = map_activity_to_learning_url(
                {"businessType": match.group(1), "businessId": match.group(2)}
            )
            if mapped:
                results.append(mapped)

        for match in re.finditer(
            rf"[\"']businessId[\"']\s*:\s*[\"']({_UUID})[\"']"
            rf".{{0,80}}?"
            rf"[\"']businessType[\"']\s*:\s*[\"']?({type_group})[\"']?",
            candidate,
            re.IGNORECASE | re.DOTALL,
        ):
            mapped = map_activity_to_learning_url(
                {"businessType": match.group(2), "businessId": match.group(1)}
            )
            if mapped:
                results.append(mapped)

    return unique_urls(results)


def _chapter_ids_from_payload(payload: object) -> list[str]:
    if isinstance(payload, list):
        chapters = payload
    elif isinstance(payload, dict):
        chapters = (
            payload.get("items")
            or payload.get("datas")
            or payload.get("data")
            or payload.get("chapters")
            or []
        )
    else:
        chapters = []

    ids: list[str] = []
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        chapter_id = str(chapter.get("id") or "").strip()
        if re.fullmatch(_UUID, chapter_id, re.IGNORECASE):
            ids.append(chapter_id)
    return ids


def _items_from_activity_payload(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        items = payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


async def _fetch_all_activities_for_chapter(
    page,
    *,
    class_id: str,
    chapter_id: str,
    headers: dict[str, str],
    status_callback=None,
) -> list[dict]:
    """
    拉取某阶段全部活动。

    实勘分页:
      page=1 → /paas
      page>=2 → /paas/more
      pageSize=5
      结束条件: 本批为空，或本批条数 < pageSize
      不使用 recordCount 作为总数（不可信）
    """
    all_items: list[dict] = []
    seen_keys: set[str] = set()
    page_no = 1

    while page_no <= 100:
        url = build_activity_list_url(
            class_id=class_id,
            chapter_id=chapter_id,
            page=page_no,
            page_size=_ACTIVITY_PAGE_SIZE,
        )
        payload = await fetch_json(page, url, headers=headers)
        items = _items_from_activity_payload(payload)
        if not items:
            break

        new_count = 0
        for item in items:
            key = str(
                item.get("id")
                or item.get("businessId")
                or f"{item.get('businessName')}-{page_no}-{new_count}"
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_items.append(item)
            new_count += 1

        if status_callback and page_no > 1:
            status_callback(
                f"阶段活动第 {page_no} 页（查看更多）+{len(items)} 条，"
                f"累计 {len(all_items)} 条"
            )

        # 末页不足 pageSize，或本页没有新数据 → 结束（对齐 UI 点完更多）
        if len(items) < _ACTIVITY_PAGE_SIZE or new_count == 0:
            break
        page_no += 1

    return all_items


async def collect_learning_links_from_class_page(
    page,
    class_url: str,
    status_callback=None,
) -> list[str]:
    """打开已登录的培训班页，经章节/活动 API（含 /more 分页）收集课程/主题链接。"""
    class_id = extract_class_id(class_url)
    if not class_id:
        if status_callback:
            status_callback(f"无法识别培训班 classId: {class_url}")
        return []

    target = (
        class_url
        if is_train_class_url(class_url) or "classId=" in class_url
        else normalize_url(class_url)
    )
    await page.goto(target, wait_until="load")
    # 培训班 SPA 可能二次跳转；稍等再关弹窗/取 token，降低 context destroyed
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        await page.wait_for_timeout(1500)
    await prepare_page_after_navigation_async(page, status_callback=status_callback)
    auth_header = await wait_for_authorization_header(
        page,
        status_callback,
        empty_message="培训班页面未拿到登录令牌，请确认 cookies 仍有效",
    )
    if not auth_header:
        return []

    headers = {
        "Authorization": auth_header,
        "Accept": "application/json, text/plain, */*",
    }

    try:
        chapters_payload = await fetch_json(
            page,
            _CHAPTER_API.format(class_id=class_id),
            headers=headers,
        )
    except Exception as exc:
        logging.error(f"读取培训班章节失败 classId={class_id}: {exc}")
        if status_callback:
            status_callback(f"读取培训班章节失败: {exc}")
        return []

    chapter_ids = _chapter_ids_from_payload(chapters_payload)
    if not chapter_ids:
        if status_callback:
            status_callback("该培训班暂无章节数据")
        return []

    if status_callback:
        status_callback(
            f"识别到 {len(chapter_ids)} 个培训阶段，正在拉取活动列表（含查看更多分页）"
        )

    all_activities: list[dict] = []
    for chapter_id in chapter_ids:
        try:
            items = await _fetch_all_activities_for_chapter(
                page,
                class_id=class_id,
                chapter_id=chapter_id,
                headers=headers,
                status_callback=status_callback,
            )
            all_activities.extend(items)
        except Exception as exc:
            logging.error(
                f"读取培训班活动失败 classId={class_id} chapterId={chapter_id}: {exc}"
            )
            if status_callback:
                status_callback(f"读取阶段活动失败，已跳过该阶段: {exc}")

    learning_links = extract_learning_links_from_activity_items(all_activities)
    mapped_count = sum(
        1 for item in all_activities if map_activity_to_learning_url(item)
    )
    skipped = len(all_activities) - mapped_count
    if status_callback and all_activities:
        status_callback(
            f"活动共 {len(all_activities)} 项，可挂机课程/主题 "
            f"{len(learning_links)} 条"
            + (f"，跳过 {skipped} 项无法映射类型" if skipped else "")
        )
    return learning_links


async def collect_learning_links_from_train_class_urls(
    train_class_urls: list[str],
    status_callback=None,
    before_close_callback=None,
    *,
    context=None,
) -> int:
    """
    打开培训班详情页，经 API（首屏 + /more）提取课程/主题链接并写入学习队列。

    可传入已有 context 复用浏览器；否则自建 context。
    """
    if not train_class_urls:
        return 0

    total_added = 0

    async def _run_with_context(active_context) -> None:
        nonlocal total_added
        for index, url in enumerate(train_class_urls, start=1):
            if status_callback:
                status_callback(
                    f"正在解析培训班链接 {index}/{len(train_class_urls)}"
                )
            page = await active_context.new_page()
            try:
                learning_links = await collect_learning_links_from_class_page(
                    page,
                    url,
                    status_callback=status_callback,
                )
                enqueue = await enqueue_learning_links_with_subject_expand(
                    learning_links,
                    page=page,
                    status_callback=status_callback,
                    source_label="培训班",
                )
                total_added += enqueue["learning_added"]
                if status_callback:
                    if learning_links:
                        status_callback(
                            "已从培训班识别 "
                            f"{enqueue['course_links']} 条课程、"
                            f"{enqueue['subject_links']} 个主题"
                            f"（展开后学习队列 +{enqueue['learning_added']}）"
                        )
                    else:
                        status_callback(
                            "该培训班暂未识别到可挂机课程/主题链接；"
                            "请确认班级活动含 course/subject 类型"
                        )
            except Exception as exc:
                logging.error(f"解析培训班失败 {url}: {exc}")
                if status_callback:
                    status_callback(f"解析培训班失败: {exc}")
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
