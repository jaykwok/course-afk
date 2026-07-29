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
import re
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
    _get_paper_attempt_limit_message,
    _has_authorization_cookie,
    _has_ready_answer_question,
    _locate_exam_button,
    _wait_for_target_route_after_auth,
)
from core.file_ops import load_cookies, normalize_url

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


def _sanitize_url(url: str) -> str:
    sanitized = re.sub(
        r"(kc\.zhixueyun\.com/oauth/#login/)[^?#\s]+",
        r"\1<redacted>",
        url,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"([?#&/](?:access_token|code)=)[^&]+",
        r"\1<redacted>",
        sanitized,
        flags=re.IGNORECASE,
    )
    return sanitized


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


async def _classify_page_state(page) -> tuple[str, str | None]:
    attempt_limit_message = await _get_paper_attempt_limit_message(page)
    if attempt_limit_message:
        return "attempt_limit", attempt_limit_message
    if await _has_ready_answer_question(page):
        return "answer_page", None
    if await _locate_exam_button(page) is not None:
        return "exam_entry", None
    return "unknown", None


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
    result_text = json.dumps(result_data, ensure_ascii=False, indent=2)
    result_file.write_text(result_text, encoding="utf-8")

    # latest.json 作为稳定入口；完整历史按页面状态分类归档。
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(result_text, encoding="utf-8")

    html_file = None
    if html is not None:
        html_file = state_dir / f"{stem}.html"
        html_file.write_text(html, encoding="utf-8")
    return result_file, html_file


async def main(target_url: Optional[str] = None):
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
        login_redirect_completed = await _wait_for_target_route_after_auth(
            page, target_url, timeout_ms=0
        )

        final_url = page.url
        returned_to_target = normalize_url(final_url) == normalize_url(target_url)
        context_cookies = await context.cookies()
        if not login_redirect_completed or not returned_to_target:
            captured_at = datetime.now()
            result_data = {
                "page_state": "auth_incomplete",
                "requested_url": _sanitize_url(target_url),
                "final_url": _sanitize_url(final_url),
                "navigation_history": [
                    _sanitize_url(url) for url in navigation_history
                ],
                "login_redirect_completed": False,
                "authorization_cookie_present": any(
                    str(item.get("name", "")).strip().lower() == "authorization"
                    for item in context_cookies
                ),
                "probed_at": captured_at.astimezone().isoformat(),
                "cookies_file": str(COOKIES_FILE),
                "cookies_loaded": len(cookies) > 0,
                "source_cookies": [
                    {
                        "name": item.get("name", ""),
                        "domain": item.get("domain", ""),
                    }
                    for item in cookies
                ],
                "context_cookies": [
                    {
                        "name": item.get("name", ""),
                        "domain": item.get("domain", ""),
                    }
                    for item in context_cookies
                ],
                "error": "授权后未回到目标考试链接，未解析或保存当前登录页",
            }
            result_file, _ = _save_classified_result(
                result_data,
                page_state="auth_incomplete",
                captured_at=captured_at,
            )
            raise RuntimeError(
                f"授权后未回到目标考试链接，已停止页面解析；诊断见 {result_file}"
            )

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
        page_state, attempt_limit_message = await _classify_page_state(page)
        captured_at = datetime.now()
        result_data = {
            "page_state": page_state,
            "requested_url": _sanitize_url(target_url),
            "final_url": _sanitize_url(final_url),
            "navigation_history": [_sanitize_url(url) for url in navigation_history],
            "title": page_title,
            "login_redirect_completed": login_redirect_completed,
            "authorization_cookie_present": await _has_authorization_cookie(page),
            "attempt_limit_message": attempt_limit_message,
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
    print(f"探针结果已保存到 {result_file}")
    print(f"页面 HTML 已保存到 {html_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="探测知学云考试页面 DOM 与授权跳转")
    parser.add_argument("url", nargs="?", help="直达考试页面 URL")
    args = parser.parse_args()
    asyncio.run(main(args.url))
