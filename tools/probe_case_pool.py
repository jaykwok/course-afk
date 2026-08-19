"""前台探测天翼专家助手案例库，不写入正式课程/考试队列。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.browser.overlays import prepare_page_after_navigation_async
from core.browser.session import create_browser_context
from core.learning.zone import (
    _collect_ctexpert_case_pool_pages,
    _ensure_ctexpert_case_pool_authenticated,
    _fetch_ctexpert_case_pool_links,
    _read_runtime_learning_links,
)


DEFAULT_URL = "https://www.ctexpert.cn/expert-assist-web/casePool"


def _status(message: str) -> None:
    print(f"CASE_POOL_STATUS: {message}", flush=True)


async def probe(url: str, hold_seconds: float) -> None:
    async with create_browser_context(headless=False) as (_, context):
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="load")
            await prepare_page_after_navigation_async(
                page,
                status_callback=_status,
            )
            print(f"CASE_POOL_STAGE: opened {page.url}", flush=True)

            await _ensure_ctexpert_case_pool_authenticated(
                page,
                url,
                status_callback=_status,
            )
            print(f"CASE_POOL_STAGE: authenticated {page.url}", flush=True)

            links, api_stats = await _fetch_ctexpert_case_pool_links(page)
            collection_mode = "api"
            page_count = api_stats["pages"]
            if links:
                _status(
                    "接口直取完成："
                    f"{api_stats['records']} 条记录 / {api_stats['pages']} 批请求"
                )
            else:
                collection_mode = "page-fallback"
                for _ in range(30):
                    links = await _read_runtime_learning_links(page)
                    if links:
                        break
                    await page.wait_for_timeout(500)
                links, page_clicks = await _collect_ctexpert_case_pool_pages(
                    page,
                    links,
                    status_callback=_status,
                )
                page_count = page_clicks + 1
            courses = sum("/study/course/detail/" in item for item in links)
            subjects = sum("/study/subject/detail/" in item for item in links)
            print(
                "CASE_POOL_RESULT: "
                + json.dumps(
                    {
                        "links": len(links),
                        "courses": courses,
                        "subjects": subjects,
                        "requests_or_pages": page_count,
                        "mode": collection_mode,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

            if hold_seconds > 0:
                _status(f"探测完成，浏览器保留 {hold_seconds:g} 秒供观察")
                await page.wait_for_timeout(int(hold_seconds * 1000))
        finally:
            await page.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="前台探测专家助手案例库课程链接")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=60,
        help="探测完成后保留浏览器窗口的秒数（默认 60）",
    )
    args = parser.parse_args()
    asyncio.run(probe(args.url, max(0, args.hold_seconds)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
