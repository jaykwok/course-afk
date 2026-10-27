from __future__ import annotations

from core.queues.learning import record_learning_failure, remove_learning_failure, remember_discovery_entries
from core.runtime import close_safely

import logging
import re
from urllib.parse import unquote

from core.browser.session import create_browser_context
from core.config import ZHIXUEYUN_COURSE_PREFIX, ZHIXUEYUN_SUBJECT_PREFIX
from core.file_ops import is_compliant_url_regex, is_train_class_url, normalize_url
from core.links import unique_urls
from core.browser.page_auth import fetch_json, wait_for_authorization_header
from core.browser.overlays import prepare_page_after_navigation_async
from core.discovery.subject_parse import enqueue_learning_links_with_subject_expand


# 实勘确认（class 活动列表）:
# - 鉴权: Authorization: Bearer__{token}
# - 章节: GET .../chapter/paas?classId=
# - 首屏: GET .../chapter-activity-list/paas?classId=&chapterId=&page=1&pageSize=5
# - 查看更多: GET .../chapter-activity-list/paas/more?...&page=2|3|...&pageSize=5
# - 首页 recordCount 不可信（本班报 5，实际 13）；以「本批条数 < pageSize」结束分页
_API_HOST = "https://kc.zhixueyun.com"
_REGISTRATION_API = (
    f"{_API_HOST}/gwapi/v1/saas-stu/web-biz/latest/train/class-student"
)
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


class ClassEnrollmentError(RuntimeError):
    """报名未确认成功，不能把班级课程当作可学习链接收集。"""


async def _registration_progress(page, class_id: str, headers: dict[str, str]) -> dict:
    payload = await fetch_json(
        page,
        f"{_REGISTRATION_API}/registrationProgress?classId={class_id}",
        headers=headers,
    )
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        raise ClassEnrollmentError("读取培训班报名状态失败，请确认登录有效后重试入口")
    progress = payload.get("data")
    if (
        not isinstance(progress, dict)
        or str(progress.get("classInfoId") or "").lower() != class_id.lower()
        or str(progress.get("registerStatus")) not in {"0", "1", "2", "3", "4", "5"}
    ):
        raise ClassEnrollmentError("培训班报名状态响应不完整，已保留入口供重试")
    return progress


def _check_pending_registration(progress: dict) -> None:
    status = str(progress.get("registerStatus"))
    if status in {"1", "3"}:
        raise ClassEnrollmentError("培训班报名待审核，审核通过后请重新添加该入口")
    if status == "4":
        raise ClassEnrollmentError("培训班报名待缴费，请在平台完成后重新添加该入口")


async def ensure_class_enrolled(
    page, class_id: str, *, headers: dict[str, str], status_callback=None,
) -> None:
    """对齐页面报名接口；提交一次，并以重新查询的 registerStatus=5 确认成功。"""
    progress = await _registration_progress(page, class_id, headers)
    if str(progress["registerStatus"]) == "5":
        if status_callback:
            status_callback("培训班已报名，继续收集课程链接")
        return
    _check_pending_registration(progress)
    options = progress.get("classInfoVo")
    if not isinstance(options, dict) or str(options.get("payType")) != "0":
        raise ClassEnrollmentError("培训班报名涉及付费或报名条件不完整，请在平台处理后重试入口")
    if str(options.get("isRegister")) == "1" and str(options.get("isRegisterReason")) == "1":
        raise ClassEnrollmentError("培训班报名需要填写报名理由，请在平台报名后重新添加该入口")
    if str(options.get("signupFile")) == "1":
        raise ClassEnrollmentError("培训班报名需要提交附件，请在平台报名后重新添加该入口")

    if status_callback:
        status_callback("培训班尚未报名，正在报名后再获取课程链接")
    payload = await fetch_json(
        page,
        f"{_REGISTRATION_API}/register",
        headers={**headers, "Content-Type": "application/json; charset=UTF-8", "permitenable": "false"},
        method="POST",
        data={"classId": class_id},
    )
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        raise ClassEnrollmentError("培训班报名失败，请在平台查看报名条件后重试入口")

    # 接口接受申请不代表获得学习权限；等待后台状态落定，不重复提交报名。
    for attempt in range(5):
        if attempt:
            await page.wait_for_timeout(500)
        progress = await _registration_progress(page, class_id, headers)
        if str(progress["registerStatus"]) == "5":
            if status_callback:
                status_callback("培训班报名成功，开始收集课程链接")
            return
        _check_pending_registration(progress)
        if str(progress["registerStatus"]) == "2":
            raise ClassEnrollmentError("培训班报名审核未通过，请在平台处理后重试入口")
    raise ClassEnrollmentError("培训班报名已提交，但尚未确认成功，已保留入口供重试")


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
        if "items" not in payload or not isinstance(payload["items"], list):
            raise ValueError("培训活动响应缺少 items 列表")
        items = payload["items"]
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
        if len(items) < _ACTIVITY_PAGE_SIZE:
            break
        if new_count == 0:
            raise RuntimeError("培训活动分页重复，收集未完成")
        page_no += 1
    else:
        raise RuntimeError("培训活动达到分页上限，收集未完成")
    return all_items


async def collect_learning_links_from_class_page(
    page,
    class_url: str,
    status_callback=None,
) -> list[str]:
    """打开培训班页，确认报名成功后，经章节/活动 API 收集课程/主题链接。"""
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
        raise ValueError("培训班缺少登录凭证，未完成收集")

    headers = {
        "Authorization": auth_header,
        "Accept": "application/json, text/plain, */*",
    }

    await ensure_class_enrolled(
        page, class_id, headers=headers, status_callback=status_callback,
    )

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
        raise

    if not isinstance(chapters_payload, list) and not (isinstance(chapters_payload, dict) and any(isinstance(chapters_payload.get(key), list) for key in ("items", "datas", "data", "chapters"))):
        raise ValueError("培训班章节响应结构不完整")
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
                status_callback(f"读取阶段活动失败，已保留入口供重试: {exc}")
            raise

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

    remember_discovery_entries(train_class_urls)
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
                remove_learning_failure(url, keep_file=True)
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
            except BaseException as exc:
                reason_text = str(exc) if isinstance(exc, ClassEnrollmentError) else f"培训班收集未完成（{type(exc).__name__}），请重新解析入口"
                record_learning_failure(url, reason="discovery_incomplete", reason_text=reason_text, detail={"source": "train_class"})
                if not isinstance(exc, Exception):
                    raise
                logging.error(f"解析培训班失败 {url}: {exc}")
                if status_callback:
                    status_callback(f"解析培训班失败: {exc}")
                raise
            finally:
                await close_safely(page.close(), label="training class page")

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
