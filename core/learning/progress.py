"""只读查询平台章节进度，复核页面进度文字未及时更新的情况。"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlencode

from core.browser.course_links import resolve_course_page_url
from core.browser.page_auth import fetch_json, get_authorization_header
from core.config import ZHIXUEYUN_COURSE_PREFIX


_SECTION_ID = re.compile(r"([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$")
_API = "https://kc.zhixueyun.com/api/v1/course-study/course-front"


async def section_id_from_box(box) -> str | None:
    try:
        # 当前 dl 的 id 为 D201show-sections{章节 UUID}，不是资源 attachmentId。
        match = _SECTION_ID.search(await box.get_attribute("id") or "")
        return match.group(1).lower() if match else None
    except Exception:
        return None


def section_completed_on_server(state: dict | None) -> bool:
    # completedRate=91 时平台也可能已完成；只信完成标记，不自行推算百分比。
    return isinstance(state, dict) and str(state.get("finish_status")) == "2"


async def read_server_section_states(page) -> dict[str, dict] | None:
    """返回按章节 ID 索引的状态；接口/来源不可验证时返回 None，不推断完成。"""
    try:
        async with asyncio.timeout(15):
            url = await resolve_course_page_url(page)
            if not url:
                return None
            parts = url.removeprefix(ZHIXUEYUN_COURSE_PREFIX).split("/")
            course_id = parts[0]
            query = {"type": parts[1] if len(parts) == 4 else "1", "sourceId": parts[3] if len(parts) == 4 else ""}
            auth = await get_authorization_header(page)
            if not auth:
                return None
            headers = {"Authorization": auth, "Accept": "application/json, text/plain, */*"}
            payload = await fetch_json(page, f"{_API}/info/{course_id}?{urlencode(query)}", headers=headers)
            info = payload.get("data", payload) if isinstance(payload, dict) else None
            if not isinstance(info, dict) or str(info.get("id") or "").lower() != course_id.lower():
                return None
            chapters = info.get("courseChapters")
            if not isinstance(chapters, list):
                return None
            resources = {}
            for chapter in chapters:
                for section in chapter.get("courseChapterSections", []) if isinstance(chapter, dict) else []:
                    if isinstance(section, dict) and section.get("id") and section.get("resourceId"):
                        resources[str(section["id"]).lower()] = str(section["resourceId"])
            if not resources:
                return None
            # 平台用 POST 做只读查询；此接口不会提交/改写学习进度。
            progress = await fetch_json(
                page, f"{_API}/course-section-progress",
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                method="POST", data=urlencode({"courseId": course_id, "resourceIds": ",".join(dict.fromkeys(resources.values()))}),
            )
            if not isinstance(progress, list):
                return None
            states = {key: {"section_id": key, "record_found": False, "finish_status": None} for key in resources}
            for record in progress:
                if not isinstance(record, dict):
                    return None
                section_id = str(record.get("sectionId") or "").lower()
                if (
                    str(record.get("courseId") or "").lower() != course_id.lower()
                    or resources.get(section_id) != str(record.get("resourceId") or "")
                    or states[section_id]["record_found"]
                ):
                    return None
                states[section_id] = {
                    "section_id": section_id, "record_found": True,
                    "finish_status": record.get("finishStatus"),
                    "completed_rate": record.get("completedRate"),
                    "study_total_time": record.get("studyTotalTime"),
                }
            return states
    except Exception as exc:
        logging.info("服务器章节进度复核不可用，保留未确认状态: %s", type(exc).__name__)
        return None


async def read_server_section_state(page, box) -> dict | None:
    section_id = await section_id_from_box(box)
    if not section_id:
        return None
    states = await read_server_section_states(page)
    return states.get(section_id) if states is not None else None
