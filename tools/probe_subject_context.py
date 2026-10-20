"""Compare course entry context without running learning or changing queues."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.browser.session import create_browser_context
from core.browser.page_auth import get_authorization_header
from core.config import COOKIES_FILE
from core.discovery.subject_parse import expand_subject_from_page, extract_subject_id
from core.diagnostics import redact_url


async def main(subject_url: str):
    subject_id = extract_subject_id(subject_url)
    if not subject_id:
        raise ValueError('Invalid subject URL')
    result = {"subject_url": subject_url, "api_comparisons": [], "navigation": []}
    async with create_browser_context(cookies_path=COOKIES_FILE, headless=False, slow_mo=0) as (_, context):
        page = await context.new_page()
        expanded = await expand_subject_from_page(page, subject_url)
        auth = await get_authorization_header(page)
        headers = {"Authorization": auth, "Version": "12.1.1"}
        for course_url in expanded.course_urls:
            course_id = course_url.split('/course/detail/', 1)[1].split('/')[0]
            comparison = {"course_id": course_id}
            for label, query in (("bare", "?type=&sourceId="), ("subject", f"?type=6&sourceId={subject_id}")):
                response = await context.request.get(
                    f"https://kc.zhixueyun.com/api/v1/course-study/course-front/info/{course_id}{query}",
                    headers=headers, max_redirects=0,
                )
                try:
                    data = await response.json()
                    comparison[label] = {"status": response.status}
                    if isinstance(data, dict):
                        comparison[label].update({k: data[k] for k in ("errorCode", "message") if k in data})
                        detail = data.get("data", data)
                        if isinstance(detail, dict):
                            comparison[label]["chapter_count"] = len(detail.get("courseChapters") or [])
                finally:
                    await response.dispose()
            result["api_comparisons"].append(comparison)
            print(json.dumps(comparison, ensure_ascii=False), flush=True)
        item = page.locator('.item.current-hover').first
        await item.wait_for()
        async with context.expect_page() as popup_info:
            await item.locator('.inline-block.operation').click()
        popup = await popup_info.value
        response = await popup.wait_for_event('response', predicate=lambda r: '/course-front/info/' in r.url, timeout=30000)
        result["navigation"].append({"kind": "subject_click", "url": redact_url(popup.url), "info_url": redact_url(response.url), "status": response.status})
        await popup.close()
        bare_url = expanded.course_urls[0].split('/6/', 1)[0]
        for label, url in (("direct_bare", bare_url), ("direct_query", f"{bare_url}?type=6&sourceId={subject_id}"), ("direct_incomplete_path", f"{bare_url}/6/{subject_id}"), ("direct_complete_path", expanded.course_urls[0])):
            direct = await context.new_page()
            async with direct.expect_response(lambda r: '/course-front/info/' in r.url, timeout=45000) as info:
                await direct.goto(url, wait_until='domcontentloaded')
            response = await info.value
            result["navigation"].append({"kind": label, "url": redact_url(direct.url), "info_url": redact_url(response.url), "status": response.status})
            print(json.dumps(result["navigation"][-1], ensure_ascii=False), flush=True)
            await direct.close()
        out = ROOT / 'tools/capture/subject_context.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result["navigation"], ensure_ascii=False), flush=True)
        print(str(out), flush=True)


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))
