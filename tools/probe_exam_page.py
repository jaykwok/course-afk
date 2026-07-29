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
from datetime import datetime
from typing import Optional

from core.browser.session import create_browser_context
from core.config import COOKIES_FILE, PROJECT_ROOT
from core.exam.runner import _wait_for_target_route_after_auth
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
        # SPA 可能先落在目标 hash，再异步发起 OAuth；给路由一次启动机会。
        await page.wait_for_timeout(1500)
        login_redirect_completed = await _wait_for_target_route_after_auth(
            page, target_url, timeout_ms=60000
        )
        await page.wait_for_timeout(1500)

        final_url = page.url
        returned_to_target = normalize_url(final_url) == normalize_url(target_url)
        context_cookies = await context.cookies()
        if not login_redirect_completed or not returned_to_target:
            CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
            RESULT_FILE.write_text(
                json.dumps(
                    {
                        "requested_url": _sanitize_url(target_url),
                        "final_url": _sanitize_url(final_url),
                        "navigation_history": [
                            _sanitize_url(url) for url in navigation_history
                        ],
                        "login_redirect_completed": False,
                        "probed_at": datetime.now().astimezone().isoformat(),
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
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            raise RuntimeError(
                f"授权后未回到目标考试链接，已停止页面解析；诊断见 {RESULT_FILE}"
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
        result_data = {
            "requested_url": _sanitize_url(target_url),
            "final_url": _sanitize_url(final_url),
            "navigation_history": [_sanitize_url(url) for url in navigation_history],
            "title": page_title,
            "login_redirect_completed": login_redirect_completed,
            "probed_at": datetime.now().astimezone().isoformat(),
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

        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        html_file = CAPTURE_DIR / f"page_{datetime.now():%Y%m%d_%H%M%S}.html"
        RESULT_FILE.write_text(
            json.dumps(result_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        html_file.write_text(html, encoding="utf-8")

    print(f"\n探针结果已保存到 {RESULT_FILE}")
    print(f"页面 HTML 已保存到 {html_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="探测知学云考试页面 DOM 与授权跳转")
    parser.add_argument("url", nargs="?", help="直达考试页面 URL")
    args = parser.parse_args()
    asyncio.run(main(args.url))
