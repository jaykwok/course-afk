import json
import logging
import os
import re
from urllib.parse import parse_qs, unquote

from core.config import (
    ZHIXUEYUN_COURSE_PREFIX,
    ZHIXUEYUN_EXAM_PREFIX,
    ZHIXUEYUN_SUBJECT_PREFIX,
    ZHIXUEYUN_TRAIN_CLASS_PREFIX,
)


def del_file(filename):
    """删除文件(如果存在)"""
    if os.path.exists(filename):
        os.remove(filename)


def normalize_text(value) -> str:
    """强制转为去首尾空白的字符串；None/空 → ''。"""
    return str(value or "").strip()


def normalize_optional_text(value) -> str | None:
    """去首尾空白；None 或空串 → None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_cookies(path) -> list:
    """读取登录凭证 cookie 文件，失败时抛出带友好提示的异常。"""
    try:
        with open(path, "r", encoding="utf-8") as file:
            cookies = json.load(file)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "未找到登录凭证文件，请先在主菜单选择「切换账号 / 更新登录凭证」完成登录。"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            "登录凭证文件已损坏，请重新登录以刷新凭证。"
        ) from exc
    except PermissionError as exc:
        raise PermissionError(
            "无法读取登录凭证文件（可能被网盘/同步软件占用），请关闭相关软件后重试。"
        ) from exc
    if isinstance(cookies, dict) and cookies.get("version") == 1:
        cookies = cookies.get("cookies")
    if not isinstance(cookies, list):
        raise ValueError("登录凭证文件格式异常（应为 cookie 列表），请重新登录。")
    return cookies


_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_BUSINESS_TYPE_PREFIX_MAP = {
    "1": ZHIXUEYUN_COURSE_PREFIX,
    "2": ZHIXUEYUN_SUBJECT_PREFIX,
    "6": ZHIXUEYUN_TRAIN_CLASS_PREFIX,
}

# 根据配置的 URL 前缀生成合规正则
_COURSE_PREFIX_ESCAPED = re.escape(ZHIXUEYUN_COURSE_PREFIX)
_SUBJECT_PREFIX_ESCAPED = re.escape(ZHIXUEYUN_SUBJECT_PREFIX)
_TRAIN_CLASS_PREFIX_ESCAPED = re.escape(ZHIXUEYUN_TRAIN_CLASS_PREFIX)
_EXAM_PREFIX_ESCAPED = re.escape(ZHIXUEYUN_EXAM_PREFIX)
_COURSE_CONTEXT = rf"/[1-9][0-9]*/[01]/{_UUID}"
_COMPLIANT_URL_PATTERN = re.compile(
    rf"^(?:{_COURSE_PREFIX_ESCAPED}{_UUID}(?:{_COURSE_CONTEXT})?|{_SUBJECT_PREFIX_ESCAPED}{_UUID})$"
)
_COURSE_DETAIL_URL_PATTERN = re.compile(
    rf"^{_COURSE_PREFIX_ESCAPED}{_UUID}(?:{_COURSE_CONTEXT})?$"
)
_SUBJECT_DETAIL_URL_PATTERN = re.compile(
    rf"^{_SUBJECT_PREFIX_ESCAPED}{_UUID}$"
)
_TRAIN_CLASS_URL_PATTERN = re.compile(
    rf"^{_TRAIN_CLASS_PREFIX_ESCAPED}{_UUID}$"
)
_EXAM_URL_PATTERN = re.compile(rf"^{_EXAM_PREFIX_ESCAPED}{_UUID}$")


def build_subject_course_url(course_id: str, subject_id: str, required: object) -> str:
    """保留专题来源及必修标记；缺失/非法参数不能降级为裸课程链接。"""
    if not re.fullmatch(_UUID, course_id or "") or not re.fullmatch(_UUID, subject_id or ""):
        raise ValueError("课程或专题 ID 非法")
    if isinstance(required, bool):
        required = int(required)
    if str(required) not in {"0", "1"}:
        raise ValueError("专题课程缺少合法的 required 标记")
    return f"{ZHIXUEYUN_COURSE_PREFIX}{course_id}/6/{required}/{subject_id}"


def normalize_url(url):
    """
    将非标准学习链接转换为标准格式。

    支持的非标准格式:
    1. qrScan格式: .../qrScan?businessType=1&businessId=UUID...  →  .../course/detail/UUID
                    .../qrScan?businessType=2&businessId=UUID...  →  .../subject/detail/UUID
                    .../qrScan?businessType=6&businessId=UUID...  →  .../train-new/class-detail/UUID
    2. paas-container 等中转: ...classId=UUID...  →  .../train-new/class-detail/UUID
    3. detail带前缀格式:
       .../detail/11&UUID...   →  .../detail/UUID
       .../detail/99@@UUID... →  .../detail/UUID

    4. 课程完整上下文路径 /UUID/type/required/sourceId 原样保留。
       完整的专题查询参数会转成该路径；缺失上下文拒绝降级。
    """
    url = (url or "").strip()
    decoded_url = unquote(url)
    candidates = list(dict.fromkeys([decoded_url, url]))

    # 课程来源是路由的一部分。先处理课程，不能由下面的通用 UUID 逻辑裁掉。
    for candidate in candidates:
        course_match = re.search(
            rf"{_COURSE_PREFIX_ESCAPED}(?:\d+(?:&|@@))?({_UUID})([^?&#]*)",
            candidate,
        )
        if not course_match:
            continue
        base = f"{ZHIXUEYUN_COURSE_PREFIX}{course_match.group(1)}"
        suffix = course_match.group(2).rstrip("/")
        query_text = candidate[course_match.end():]
        params = parse_qs(query_text[1:], keep_blank_values=True) if query_text.startswith("?") else {}
        context_keys = {"type", "sourceId", "required", "require", "isRequired"}
        if suffix:
            if re.fullmatch(_COURSE_CONTEXT, suffix) and not context_keys.intersection(params):
                return base + suffix
            return candidate  # 不完整/冲突的上下文不能静默变成可执行裸链接。
        if context_keys.intersection(params):
            required_values = [params[key] for key in ("required", "require", "isRequired") if key in params]
            if (
                params.get("type") == ["6"]
                and len(params.get("sourceId", [])) == 1
                and len(required_values) == 1 and len(required_values[0]) == 1
            ):
                try:
                    return build_subject_course_url(course_match.group(1), params["sourceId"][0], required_values[0][0])
                except ValueError:
                    pass
            return candidate
        return base

    # 已知详情链接只保留末尾 UUID。兼容平台在 UUID 前插入的数字标记，
    # 例如 /detail/99@@UUID、/detail/11&UUID。
    for candidate in candidates:
        standard_match = re.search(
            rf"({_SUBJECT_PREFIX_ESCAPED}|"
            rf"{_TRAIN_CLASS_PREFIX_ESCAPED}|{_EXAM_PREFIX_ESCAPED})"
            rf"(?:\d+(?:&|@@))?({_UUID})(?=$|[?&#])",
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
            type_code = business_match.group(1)
            prefix = _BUSINESS_TYPE_PREFIX_MAP.get(type_code)
            if prefix:
                return f"{prefix}{business_match.group(2)}"
            logging.warning(
                f"未知 businessType={type_code}，链接未能归一化为标准格式: {url}"
            )

    # paas-container 等: classId=UUID → 培训班详情。
    for candidate in candidates:
        class_match = re.search(rf"classId=({_UUID})", candidate, re.IGNORECASE)
        if class_match:
            return f"{ZHIXUEYUN_TRAIN_CLASS_PREFIX}{class_match.group(1)}"

    return url


def is_compliant_url_regex(url):
    """
    使用正则表达式判断URL是否符合指定的合规格式。

    课程支持裸 UUID 及完整 /type/required/sourceId；专题仍为裸 UUID。
    """

    return bool(_COMPLIANT_URL_PATTERN.match(normalize_url(url) if url else ""))


def is_course_detail_url(url: str) -> bool:
    """判断 URL 是否为标准课程详情链接（非主题）。"""

    return bool(_COURSE_DETAIL_URL_PATTERN.match(normalize_url(url)))


def is_subject_detail_url(url: str) -> bool:
    """判断 URL 是否为标准主题详情链接。"""

    return bool(_SUBJECT_DETAIL_URL_PATTERN.match(normalize_url(url)))


def is_train_class_url(url: str) -> bool:
    """判断 URL 是否为标准培训班详情链接。"""

    return bool(_TRAIN_CLASS_URL_PATTERN.match(normalize_url(url)))


def is_exam_url(url: str) -> bool:
    """判断 URL 是否为标准智学云试卷答题链接。"""

    return bool(_EXAM_URL_PATTERN.match(normalize_url(url)))
