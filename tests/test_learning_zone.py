import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from core.learning.zone import (
    collect_learning_links_from_learning_zone_urls,
    extract_learning_links_from_learning_zone_html,
)


class LearningZoneParsingTests(unittest.TestCase):
    def test_extract_learning_links_from_learning_zone_html_parses_app_links(self):
        html = """
        <html>
          <body>
            <a href="https://kc.zhixueyun.com/app/#/resource?businessType=1&businessId=11111111-1111-1111-1111-111111111111">课程A</a>
            <a href="https://kc.zhixueyun.com/app/#/resource?businessType=2&businessId=22222222-2222-2222-2222-222222222222">主题B</a>
            <a href="https://kc.zhixueyun.com/#/study/course/detail/33333333-3333-3333-3333-333333333333">课程C</a>
            <a href="https://kc.zhixueyun.com/#/study/course/detail/33333333-3333-3333-3333-333333333333">课程C重复</a>
            <a href="https://example.com/ignore">忽略</a>
          </body>
        </html>
        """

        self.assertEqual(
            extract_learning_links_from_learning_zone_html(html),
            [
                "https://kc.zhixueyun.com/#/study/course/detail/11111111-1111-1111-1111-111111111111",
                "https://kc.zhixueyun.com/#/study/subject/detail/22222222-2222-2222-2222-222222222222",
                "https://kc.zhixueyun.com/#/study/course/detail/33333333-3333-3333-3333-333333333333",
            ],
        )

    def test_extract_also_finds_detail_paths_in_raw_html_text(self):
        html = """
        <html><body>
          <div data-url="#/study/subject/detail/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"></div>
          var x = 'https://kc.zhixueyun.com/#/study/course/detail/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
        </body></html>
        """
        links = extract_learning_links_from_learning_zone_html(html)
        self.assertIn(
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            links,
        )
        self.assertIn(
            "https://kc.zhixueyun.com/#/study/course/detail/"
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            links,
        )


class FakeLearningZonePage:
    def __init__(self):
        # prepare 多轮 + 轮询内 dismiss：首次关到弹窗，之后 evaluate 均返回 None
        self.overlay_results = [
            {"tag": "svg", "className": "close-button", "zIndex": 999999},
        ]
        self.html_results = [
            "<html><body></body></html>",
            '<a href="https://kc.zhixueyun.com/#/study/subject/detail/'
            '22222222-2222-2222-2222-222222222222">课程</a>',
        ]
        self.waits = []
        self.closed = False
        self.more_calls = 0

    async def goto(self, _url, wait_until=None):
        self.wait_until = wait_until

    async def evaluate(self, script):
        # 点击更多脚本
        if "查看更多" in (script or "") or "labels" in (script or ""):
            self.more_calls += 1
            return {"ok": False}
        if "document.body" in (script or "") and "innerText" in (script or ""):
            return ""
        return self.overlay_results.pop(0) if self.overlay_results else None

    async def content(self):
        return self.html_results.pop(0) if self.html_results else (
            '<a href="https://kc.zhixueyun.com/#/study/subject/detail/'
            '22222222-2222-2222-2222-222222222222">课程</a>'
        )

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    async def close(self):
        self.closed = True


class FakeLearningZoneContext:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


class LearningZoneCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_collection_closes_popup_and_waits_for_dynamic_links(self):
        page = FakeLearningZonePage()
        context = FakeLearningZoneContext(page)
        messages = []
        lifecycle = {"exited": False, "callback": None}

        @asynccontextmanager
        async def fake_browser_context():
            try:
                yield object(), context
            finally:
                lifecycle["exited"] = True

        enqueue_calls: list[list[str]] = []

        async def fake_enqueue(
            learning_links,
            *,
            page=None,
            status_callback=None,
            source_label: str = "来源",
        ):
            enqueue_calls.append(list(learning_links))
            return {
                "course_links": 0,
                "subject_links": 1,
                "course_added": 0,
                "subject_learning_added": 1,
                "learning_added": 1,
                "exam_added": 0,
            }

        with (
            patch(
                "core.learning.zone.create_browser_context",
                side_effect=fake_browser_context,
            ),
            patch(
                "core.learning.zone.enqueue_learning_links_with_subject_expand",
                side_effect=fake_enqueue,
            ),
        ):
            added = await collect_learning_links_from_learning_zone_urls(
                ["https://cms.mylearning.cn/zone.html"],
                status_callback=messages.append,
                before_close_callback=lambda count: lifecycle.update(
                    callback=(count, lifecycle["exited"])
                ),
            )

        self.assertEqual(added, 1)
        self.assertEqual(len(enqueue_calls), 1)
        self.assertIn(
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "22222222-2222-2222-2222-222222222222",
            enqueue_calls[0],
        )
        # prepare 关弹窗 settle 200 + 轮间 400；轮询未命中链接时再 500
        self.assertIn(200, page.waits)
        self.assertIn(400, page.waits)
        self.assertIn(500, page.waits)
        self.assertTrue(page.closed)
        self.assertTrue(any("已关闭 1 个页面弹窗" in item for item in messages))
        self.assertTrue(any("0 条课程、1 个主题" in item for item in messages))
        self.assertEqual(lifecycle["callback"], (1, False))
        self.assertTrue(lifecycle["exited"])

    async def test_collection_reuses_external_context_without_creating_browser(self):
        page = FakeLearningZonePage()
        context = FakeLearningZoneContext(page)

        with (
            patch(
                "core.learning.zone.create_browser_context",
            ) as mock_create,
            patch(
                "core.learning.zone.enqueue_learning_links_with_subject_expand",
                new=AsyncMock(
                    return_value={
                        "course_links": 0,
                        "subject_links": 1,
                        "course_added": 0,
                        "subject_learning_added": 1,
                        "learning_added": 1,
                        "exam_added": 0,
                    }
                ),
            ),
        ):
            added = await collect_learning_links_from_learning_zone_urls(
                ["https://cms.mylearning.cn/zone.html"],
                context=context,
            )

        self.assertEqual(added, 1)
        mock_create.assert_not_called()
        self.assertTrue(page.closed)


if __name__ == "__main__":
    unittest.main()
