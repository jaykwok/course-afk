import logging
import re

# 题型关键词映射
QUESTION_TYPE_MAP = {
    "单选题": "single",
    "多选题": "multiple",
    "不定项选择": "multiple",
    "判断题": "judge",
    "填空题": "fill_blank",
    "排序题": "ordering",
    "阅读理解题": "reading",
}

CHOICE_OPTION_SPECS = [
    {
        "item_selector": ".preview-list dd",
        "click_selector": ".preview-list dd",
        "label_selectors": [".option-num", ".label", ".option-label", ".num"],
        "text_selectors": [
            ".answer-options",
            ".option-content",
            ".content",
            ".answer-text",
            ".label-text",
            ".text",
        ],
    },
    {
        "item_selector": ".preview-list .option-item",
        "click_selector": ".preview-list .option-item",
        "label_selectors": [".option-num", ".label", ".option-label", ".num"],
        "text_selectors": [
            ".answer-options",
            ".option-content",
            ".content",
            ".answer-text",
            ".label-text",
            ".text",
        ],
    },
    {
        "item_selector": ".option-item",
        "click_selector": ".option-item",
        "label_selectors": [".option-num", ".label", ".option-label", ".num"],
        "text_selectors": [
            ".answer-options",
            ".option-content",
            ".content",
            ".answer-text",
            ".label-text",
            ".text",
        ],
    },
    {
        "item_selector": ".answer-list .option-item",
        "click_selector": ".answer-list .option-item",
        "label_selectors": [".option-num", ".label", ".option-label", ".num"],
        "text_selectors": [
            ".answer-options",
            ".option-content",
            ".content",
            ".answer-text",
            ".label-text",
            ".text",
        ],
    },
    {
        "item_selector": ".answer-item",
        "click_selector": ".answer-item",
        "label_selectors": [".option-num", ".label", ".option-label", ".num"],
        "text_selectors": [
            ".answer-options",
            ".option-content",
            ".content",
            ".answer-text",
            ".label-text",
            ".text",
        ],
    },
]

JUDGE_OPTION_SPECS = [
    {
        "item_selector": ".preview-list dd .pointer",
        "click_selector": ".preview-list dd .pointer",
    },
    {
        "item_selector": ".preview-list dd span.pointer",
        "click_selector": ".preview-list dd span.pointer",
    },
    {
        "item_selector": ".option-item",
        "click_selector": ".option-item",
    },
]


async def _safe_inner_text(locator) -> str:
    try:
        return (await locator.inner_text()).strip()
    except Exception:
        return ""


def _normalize_option_label(raw_label: str, index: int) -> str:
    if not raw_label:
        return chr(ord("A") + index)

    upper = raw_label.upper()
    if "正确" in raw_label:
        return "T"
    if "错误" in raw_label:
        return "F"

    match = re.search(r"[A-Z]", upper)
    if match:
        return match.group(0)
    return chr(ord("A") + index)


def _strip_label_prefix(text: str, label: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""

    patterns = [
        rf"^\s*{re.escape(label)}\s*[\.、:：\)\]]?\s*",
        rf"^\s*\(?{re.escape(label)}\)?\s*",
    ]
    for pattern in patterns:
        stripped = re.sub(pattern, "", stripped, count=1, flags=re.IGNORECASE)
    return stripped.strip()


async def _extract_text_from_selectors(locator, selectors: list[str]) -> str:
    for selector in selectors:
        candidate = locator.locator(selector)
        if await candidate.count() <= 0:
            continue
        text = await _safe_inner_text(candidate)
        if text:
            return text
    return ""


async def _extract_choice_options(locator, specs: list[dict]) -> tuple[list[dict], str | None]:
    for spec in specs:
        option_elements = locator.locator(spec["item_selector"])
        count = await option_elements.count()
        if count <= 0:
            continue

        options = []
        has_text = False
        for i in range(count):
            option_element = option_elements.nth(i)
            raw_label = await _extract_text_from_selectors(
                option_element, spec["label_selectors"]
            )
            label = _normalize_option_label(raw_label, i)

            option_text = await _extract_text_from_selectors(
                option_element, spec["text_selectors"]
            )
            if not option_text:
                option_text = _strip_label_prefix(
                    await _safe_inner_text(option_element),
                    label,
                )
            if option_text:
                has_text = True

            options.append(
                {
                    "label": label,
                    "text": option_text,
                }
            )

        if has_text:
            logging.debug(f"选项提取命中选择器: {spec['item_selector']}")
            return options, spec["click_selector"]

    return [], None


async def _extract_judge_options(locator) -> tuple[list[dict], str | None]:
    for spec in JUDGE_OPTION_SPECS:
        option_elements = locator.locator(spec["item_selector"])
        count = await option_elements.count()
        if count <= 0:
            continue

        options = []
        for i in range(count):
            option_text = await _safe_inner_text(option_elements.nth(i))
            if not option_text:
                continue
            options.append(
                {
                    "label": "T" if "正确" in option_text else "F",
                    "text": option_text,
                }
            )

        if options:
            logging.debug(f"判断题选项提取命中选择器: {spec['item_selector']}")
            return options, spec["click_selector"]

    return [], None


def parse_question_type(type_text: str) -> str:
    """根据题型文本解析题目类型"""
    for keyword, qtype in QUESTION_TYPE_MAP.items():
        if keyword in type_text:
            return qtype
    return "unknown"


async def detect_question_type_by_dom(locator) -> str:
    """通过 DOM 结构检测题型(文本解析失败时的后备方案)"""
    if await locator.locator("form.vertical .sentence-input").count() > 0:
        return "fill_blank"
    if await locator.locator(".answer-input-shot").count() > 0:
        return "ordering"
    return "unknown"


async def extract_options_with_selector(locator, question_type: str) -> tuple[list[dict], str | None]:
    if question_type == "fill_blank":
        logging.info("检测到填空题, 跳过选项提取")
        return [], None

    if question_type == "judge":
        return await _extract_judge_options(locator)

    return await _extract_choice_options(locator, CHOICE_OPTION_SPECS)


async def extract_options(locator, question_type: str) -> list[dict]:
    """
    从题目容器中提取选项列表。

    Args:
        locator: 题目容器的 Playwright Locator(单题模式为 page, 多题模式为 question_item)
        question_type: 题目类型字符串
    """
    options, _ = await extract_options_with_selector(locator, question_type)
    return options
