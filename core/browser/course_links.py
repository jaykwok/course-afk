"""Resolve platform temporary course routes before saving them outside the browser."""
from __future__ import annotations

import asyncio
import logging

from core.config import ZHIXUEYUN_COURSE_PREFIX
from core.file_ops import build_subject_course_url, is_course_detail_url, normalize_url


async def resolve_course_page_url(page) -> str | None:
    raw_url = getattr(page, "url", "")
    if not isinstance(raw_url, str):
        return None
    normalized = normalize_url(raw_url)
    if is_course_detail_url(normalized):
        return normalized
    if not raw_url.startswith(ZHIXUEYUN_COURSE_PREFIX + "detailInfo"):
        return None
    try:
        # 平台 open-url 将真实参数存入 rParams，URL 中只有临时索引。
        # 仅读取当前索引的白名单字段；不导出账号或认证数据。
        async with asyncio.timeout(5):
            info = await page.evaluate("""() => {
            const key = location.hash.split('/course/detail/')[1]?.split('/')[0];
            const getInfo = window.zxyDrizzle?.global?.rateMethods?.getInfo;
            if (!key || !getInfo) return null;
            for (const storageKey of Object.keys(localStorage).filter(k => k.startsWith('rParams'))) {
                let stored;
                try { stored = JSON.parse(localStorage.getItem(storageKey)); } catch (_) { continue; }
                for (const [module, entries] of Object.entries(stored || {})) {
                    if (!entries?.[key]) continue;
                    const data = getInfo({id: key}, module, 'id');
                    if (!data) return null;
                    return {id: data.id, type: data.type, required: data.isRequired ?? data.required ?? data.require, sourceId: data.sourceId ?? data.subjectId};
                }
            }
            return null;
        }""")
        if not isinstance(info, dict):
            return None
        if str(info.get("type")) == "6":
            return build_subject_course_url(info.get("id"), info.get("sourceId"), info.get("required"))
        # 未知来源不可用裸链接替代。普通课程只接受明确的独立入口。
        if str(info.get("type")) == "1" and not info.get("sourceId"):
            candidate = ZHIXUEYUN_COURSE_PREFIX + str(info.get("id") or "")
            return candidate if is_course_detail_url(candidate) else None
    except Exception as exc:
        logging.debug("无法解析临时课程入口: %s", type(exc).__name__)
    return None
