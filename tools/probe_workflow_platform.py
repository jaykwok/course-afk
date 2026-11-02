"""Authorized read-only platform probe. Never exports cookies or raw responses."""

from pathlib import Path
import argparse
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def probe(courses, classes):
    from core.browser.session import create_browser_context
    from core.browser.page_auth import get_authorization_header
    from core.discovery.train_class import extract_class_id, _registration_progress
    from core.learning.progress import read_server_section_states
    from core.learning.playback import read_playback_snapshot
    from core.file_ops import normalize_url
    from core.runtime import close_safely

    result = {
        "scope": "read-only live platform; no enrollment, study click or exam submission",
        "courses": [],
        "classes": [],
    }
    async with create_browser_context(controller=False) as (_, context):
        for kind, urls in (("courses", courses), ("classes", classes)):
            for url in urls:
                page = await context.new_page()
                item = {}
                try:
                    await page.goto(
                        normalize_url(url), wait_until="load", timeout=30000
                    )
                    await page.wait_for_timeout(5000)
                    if kind == "courses":
                        states = await read_server_section_states(page)
                        from collections import Counter
                        from core.browser.course_links import observed_course_source

                        course_id = (
                            normalize_url(url).split("/detail/")[-1].split("/")[0]
                        )
                        item = {
                            "authorization_available": bool(
                                await get_authorization_header(page)
                            ),
                            "observed_source": observed_course_source(page, course_id),
                            "server_state_available": states is not None,
                            "sections": len(states or {}),
                            "finish_statuses": dict(
                                Counter(
                                    str(v.get("finish_status"))
                                    for v in (states or {}).values()
                                )
                            ),
                        }
                        boxes = page.locator("dl.chapter-list-box")
                        if await boxes.count():
                            snapshot = await read_playback_snapshot(page, boxes.first)
                            item["playback"] = {
                                k: snapshot.get(k)
                                for k in (
                                    "target_focused",
                                    "start_gate_visible",
                                    "content_visible",
                                    "document_focused",
                                    "ready",
                                )
                            }
                    else:
                        auth = await get_authorization_header(page)
                        if not auth:
                            item = {"authorization_available": False}
                        else:
                            progress = await _registration_progress(
                                page,
                                extract_class_id(url),
                                {"Authorization": auth, "Accept": "application/json"},
                            )
                            item = {
                                "authorization_available": True,
                                "registration_status": str(
                                    progress.get("registerStatus")
                                ),
                            }
                except Exception as exc:
                    item = {"error_type": type(exc).__name__}
                finally:
                    await close_safely(page.close(), label="read-only probe page")
                result[kind].append(item)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course", action="append", default=[])
    parser.add_argument("--first-pending-course", action="store_true")
    parser.add_argument("--class-url", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.first_pending_course:
        from core.config import LEARNING_URLS_FILE
        from core.file_ops import is_course_detail_url

        from core.queues.learning import read_learning_urls

        candidates = [url for url in read_learning_urls(LEARNING_URLS_FILE) if is_course_detail_url(url)]
        if candidates:
            args.course.append(candidates[0])
    result = asyncio.run(probe(args.course, args.class_url))
    from core.storage import write_json_atomic

    write_json_atomic(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
