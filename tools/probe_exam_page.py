#!/usr/bin/env python3
"""
知学云考试页面探针（使用正式流程的凭证与授权等待规则）。

用法：
    uv run python tools/probe_exam_page.py <exam-url>

结果保存到 ``tools/capture/exam_page/``。
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.browser.session import create_browser_context
from core.config import COOKIES_FILE, PROJECT_ROOT
from core.exam.runner import (
    _has_authorization_cookie,
    _has_ready_answer_question,
    _wait_for_target_route_after_auth,
)
from core.file_ops import load_cookies, normalize_url
from core.exam.entry_api import read_exam_entry, validate_exam_entry

CAPTURE_DIR = PROJECT_ROOT / "tools" / "capture" / "exam_page"
RESULT_FILE = CAPTURE_DIR / "latest.json"
SELECTORS = [
    ".banner-handler-btn.themeColor-border-color.themeColor-background-color",
    "button:has-text('开始考试')",
    "button:has-text('继续考试')",
    "button:has-text('去考试')",
    "a:has-text('开始考试')",
    "a:has-text('继续考试')",
    "a:has-text('去考试')",
    ".btn.new-radius",
    ".question-type-item, .single-title, .single-btns",
    "[data-region='modal:modal']",
]


from core.diagnostics import redact_url as _sanitize_url, structural_html, redact_snapshot


async def _inspect_selector(page, selector: str) -> dict[str, object]:
    locator = page.locator(selector)
    count = await locator.count()
    visible_count = 0
    texts: list[str] = []
    for index in range(min(count, 10)):
        candidate = locator.nth(index)
        if await candidate.is_visible():
            visible_count += 1
        text = (await candidate.inner_text()).strip()
        if text:
            texts.append(text[:300])
    return {
        "selector": selector,
        "count": count,
        "visible_count": visible_count,
        "texts": texts,
    }


def _save_classified_result(
    result_data: dict[str, object],
    *,
    page_state: str,
    captured_at: datetime,
    html: str | None = None,
) -> tuple[Path, Path | None]:
    state_dir = CAPTURE_DIR / page_state
    state_dir.mkdir(parents=True, exist_ok=True)
    stem = f"probe_{captured_at:%Y%m%d_%H%M%S_%f}"
    result_file = state_dir / f"{stem}.json"
    result_text = json.dumps(redact_snapshot(result_data), ensure_ascii=False, indent=2)
    result_file.write_text(result_text, encoding="utf-8")

    # latest.json 作为稳定入口；完整历史按页面状态分类归档。
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(result_text, encoding="utf-8")

    html_file = None
    if html is not None:
        html_file = state_dir / f"{stem}.html"
        html_file.write_text(structural_html(html), encoding="utf-8")
    return result_file, html_file


async def main(target_url: Optional[str] = None, *, validate_entry: bool = False):
    target_url = (target_url or input("请输入考试页面 URL：")).strip()
    if not target_url:
        raise ValueError("未提供考试页面 URL")

    print(f"正在打开页面：{_sanitize_url(target_url)}")
    print("完全参照考试流程加载 cookie...\n")

    cookies = load_cookies(COOKIES_FILE)

    async with create_browser_context(cookies_path=COOKIES_FILE, headless=False) as (_, context):
        page = await context.new_page()
        navigation_history: list[str] = []
        page.on(
            "framenavigated",
            lambda frame: navigation_history.append(frame.url)
            if frame == page.main_frame
            else None,
        )
        await page.goto(target_url, wait_until="load")
        ready = await _wait_for_target_route_after_auth(
            page, target_url, timeout_ms=120000, require_entry_dom=False,
        )
        if not ready:
            raise RuntimeError("考试入口认证超时，已停止探针")

        final_url = page.url
        if normalize_url(final_url) != normalize_url(target_url):
            raise RuntimeError(
                "授权等待完成后页面再次离开目标考试链接；"
                "已停止解析，且未保存当前页面"
            )
        context_cookies = await context.cookies()
        entry = await read_exam_entry(page, target_url)
        validation_passed = None
        if validate_entry:
            await validate_exam_entry(page, entry)
            validation_passed = True

        selector_results = []
        for selector in SELECTORS:
            result = await _inspect_selector(page, selector)
            selector_results.append(result)
            print(
                f"{selector}: count={result['count']} "
                f"visible={result['visible_count']}"
            )

        page_title = await page.title()
        body_text = (await page.locator("body").inner_text()).strip()
        html = await page.content()
        page_state = (
            "answer_page" if await _has_ready_answer_question(page)
            else "entry_blocked" if entry.blocked_reason else "exam_entry"
        )
        attempt_limit_message = entry.blocked_message if entry.blocked_reason == "attempt_limit" else None
        captured_at = datetime.now()
        result_data = {
            "page_state": page_state,
            "requested_url": _sanitize_url(target_url),
            "final_url": _sanitize_url(final_url),
            "navigation_history": [_sanitize_url(url) for url in navigation_history],
            "title": page_title,
            "authorization_cookie_present": await _has_authorization_cookie(page),
            "attempt_limit_message": attempt_limit_message,
            "entry_api": {
                "exam_id": entry.exam_id,
                "answer_url": entry.answer_url,
                "can_resume": entry.can_resume,
                "blocked_reason": entry.blocked_reason,
                "blocked_message": entry.blocked_message,
                "allowed_attempts": entry.state.allowed_attempts,
                "used_attempts": entry.state.used_attempts,
                "remaining_attempts": entry.state.remaining_attempts,
                "outcome": entry.state.outcome,
                "validation_passed": validation_passed,
            },
            "probed_at": captured_at.astimezone().isoformat(),
            "cookies_file": str(COOKIES_FILE),
            "cookies_loaded": len(cookies) > 0,
            "source_cookies": [
                {"name": item.get("name", ""), "domain": item.get("domain", "")}
                for item in cookies
            ],
            "context_cookies": [
                {"name": item.get("name", ""), "domain": item.get("domain", "")}
                for item in context_cookies
            ],
            "body_text": body_text[:5000],
            "selectors": selector_results,
        }

        result_file, html_file = _save_classified_result(
            result_data,
            page_state=page_state,
            captured_at=captured_at,
            html=html,
        )

    print(f"\n页面状态: {page_state}")
    print(f"接口解析的答题地址: {entry.answer_url}")
    print(f"接口续考状态: {entry.can_resume}")
    print(f"探针结果已保存到 {result_file}")
    print(f"页面 HTML 已保存到 {html_file}")


if __name__ == "__main__":
    from core.runtime import protect_application_children
    protect_application_children()
    parser = argparse.ArgumentParser(description="探测知学云考试页面 DOM 与授权跳转")
    parser.add_argument("url", nargs="?", help="直达考试页面 URL")
    parser.add_argument("--validate-entry", action="store_true", help="额外执行平台资格校验，不打开答题页")
    args = parser.parse_args()
    asyncio.run(main(args.url, validate_entry=args.validate_entry))
