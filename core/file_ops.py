import logging
import os
import re
from urllib.parse import unquote

from core.config import (
    ZHIXUEYUN_COURSE_PREFIX,
    ZHIXUEYUN_EXAM_PREFIX,
    ZHIXUEYUN_SUBJECT_PREFIX,
)


def del_file(filename):
    """删除文件(如果存在)"""
    if os.path.exists(filename):
        os.remove(filename)


def save_to_file(filename, url):
    """将链接保存到指定文件"""

    with open(filename, "a", encoding="utf-8") as wp:
        wp.write(f"{url}\n")
    logging.info(f"写入 {filename} 完毕")


def read_unique_lines(filename) -> list[str]:
    try:
        with open(filename, "r", encoding="utf-8") as file:
            lines = [line.strip() for line in file if line.strip()]
            return list(dict.fromkeys(lines))
    except FileNotFoundError:
        return []


def write_unique_lines(filename, urls: list[str], *, keep_file: bool = True) -> None:
    unique_urls = list(dict.fromkeys(url.strip() for url in urls if url and url.strip()))
    if not unique_urls and not keep_file:
        del_file(filename)
        return

    with open(filename, "w", encoding="utf-8") as file:
        for url in unique_urls:
            file.write(f"{url}\n")


def append_unique_lines(filename, urls: list[str]) -> list[str]:
    existing = set(read_unique_lines(filename))
    added: list[str] = []
    with open(filename, "a", encoding="utf-8") as file:
        for raw_url in urls:
            url = raw_url.strip()
            if not url or url in existing:
                continue
            file.write(f"{url}\n")
            existing.add(url)
            added.append(url)
    return added


_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_BUSINESS_TYPE_MAP = {"1": "course", "2": "subject"}

# 根据配置的 URL 前缀生成合规正则
_COURSE_PREFIX_ESCAPED = re.escape(ZHIXUEYUN_COURSE_PREFIX)
_SUBJECT_PREFIX_ESCAPED = re.escape(ZHIXUEYUN_SUBJECT_PREFIX)
_EXAM_PREFIX_ESCAPED = re.escape(ZHIXUEYUN_EXAM_PREFIX)
_COMPLIANT_URL_PATTERN = re.compile(
    rf"^({_COURSE_PREFIX_ESCAPED}|{_SUBJECT_PREFIX_ESCAPED}){_UUID}$"
)
_EXAM_URL_PATTERN = re.compile(rf"^{_EXAM_PREFIX_ESCAPED}{_UUID}$")


def normalize_url(url):
    """
    将非标准学习链接转换为标准格式。

    支持的非标准格式:
    1. qrScan格式: .../qrScan?businessType=1&businessId=UUID...  →  .../course/detail/UUID
                    .../qrScan?businessType=2&businessId=UUID...  →  .../subject/detail/UUID
    2. detail带前缀格式: .../detail/11&UUID...  →  .../detail/UUID

    已是标准格式的链接原样返回。
    """
    url = (url or "").strip()
    decoded_url = unquote(url)
    candidates = list(dict.fromkeys([url, decoded_url]))

    # 标准 detail 链接可能带有尾部查询参数，保存时只保留稳定的 UUID 链接。
    for candidate in candidates:
        standard_match = re.search(
            rf"({_COURSE_PREFIX_ESCAPED}|{_SUBJECT_PREFIX_ESCAPED}|{_EXAM_PREFIX_ESCAPED})({_UUID})",
            candidate,
        )
        if standard_match:
            return f"{standard_match.group(1)}{standard_match.group(2)}"

    # qrScan/app/中转参数格式: 从参数中提取 businessType 和 businessId。
    for candidate in candidates:
        business_match = re.search(
            rf"businessType=(\d+).*?businessId=({_UUID})",
            candidate,
        )
        if business_match:
            btype = _BUSINESS_TYPE_MAP.get(business_match.group(1))
            if btype:
                prefix = (
                    ZHIXUEYUN_COURSE_PREFIX
                    if btype == "course"
                    else ZHIXUEYUN_SUBJECT_PREFIX
                )
                return f"{prefix}{business_match.group(2)}"

    # detail带前缀格式: /detail/数字&UUID → /detail/UUID
    for candidate in candidates:
        detail_match = re.search(rf"/detail/\d+&({_UUID})", candidate)
        if detail_match:
            prefix = candidate[: candidate.index("/detail/")]
            return f"{prefix}/detail/{detail_match.group(1)}"

    return url


def is_compliant_url_regex(url):
    """
    使用正则表达式判断URL是否符合指定的合规格式。

    合规格式: https://kc.zhixueyun.com/#/study/(course|subject)/detail/UUID
    """

    return bool(_COMPLIANT_URL_PATTERN.match(url))


def is_exam_url(url: str) -> bool:
    """判断 URL 是否为标准智学云试卷答题链接。"""

    return bool(_EXAM_URL_PATTERN.match(normalize_url(url)))
