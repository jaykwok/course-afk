import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from core.learning_zone import (
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


class FakeLearningZonePage:
    def __init__(self):
        self.overlay_results = [
            {"tag": "svg", "className": "close-button", "zIndex": 999999},
            None,
            None,
        ]
        self.html_results = [
            "<html><body></body></html>",
            '<a href="https://kc.zhixueyun.com/#/study/subject/detail/'
            '22222222-2222-2222-2222-222222222222">课程</a>',
        ]
        self.waits = []
        self.closed = False

    async def goto(self, _url, wait_until=None):
        self.wait_until = wait_until

    async def evaluate(self, _script):
        return self.overlay_results.pop(0) if self.overlay_results else None

    async def content(self):
        return self.html_results.pop(0)

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

        @asynccontextmanager
        async def fake_browser_context():
            yield object(), context

        with (
            patch(
                "core.learning_zone.create_browser_context",
                side_effect=fake_browser_context,
            ),
            patch(
                "core.learning_zone.append_learning_urls",
                return_value=[
                    "https://kc.zhixueyun.com/#/study/subject/detail/"
                    "22222222-2222-2222-2222-222222222222"
                ],
            ),
        ):
            added = await collect_learning_links_from_learning_zone_urls(
                ["https://cms.mylearning.cn/zone.html"],
                status_callback=messages.append,
            )

        self.assertEqual(added, 1)
        self.assertEqual(page.waits, [200, 500])
        self.assertTrue(page.closed)
        self.assertTrue(any("已关闭 1 个页面弹窗" in item for item in messages))
        self.assertTrue(any("识别 1 条链接" in item for item in messages))


if __name__ == "__main__":
    unittest.main()
