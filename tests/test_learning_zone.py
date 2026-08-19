import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from core.learning import zone as learning_zone_module
from core.learning.zone import (
    collect_learning_links_from_learning_zone_urls,
    extract_learning_links_from_learning_zone_html,
    extract_learning_links_from_runtime_values,
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

    def test_extract_case_pool_vue_runtime_card_urls(self):
        values = [
            "https://kc.zhixueyun.com/#/study/course/detail/"
            "11111111-1111-1111-1111-111111111111",
            "https://kc.zhixueyun.com/app/#/resource?businessType=2&"
            "businessId=22222222-2222-2222-2222-222222222222",
            "https://www.ctexpert.cn/unrelated",
        ]

        self.assertEqual(
            extract_learning_links_from_runtime_values(values),
            [
                "https://kc.zhixueyun.com/#/study/course/detail/"
                "11111111-1111-1111-1111-111111111111",
                "https://kc.zhixueyun.com/#/study/subject/detail/"
                "22222222-2222-2222-2222-222222222222",
            ],
        )


class FakeCasePoolSsoPage:
    def __init__(self):
        self.url = "https://www.ctexpert.cn/expert-assist-web/casePool"
        self.frames = [self]
        self.clicked = False
        self.has_token = False
        self.waits = []

    async def evaluate(self, script):
        if "localStorage.getItem('userID')" in script:
            return {"hasToken": self.has_token, "hasAuthCode": False}
        if "const exact = new Set" in script:
            if self.clicked:
                return {"ok": False}
            self.clicked = True
            self.url = "https://sso.example.test/authorize"
            return {"ok": True, "text": "登录"}
        raise AssertionError("unexpected script")

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        if self.clicked:
            self.has_token = True
            self.url = "https://www.ctexpert.cn/expert-assist-web/casePool"


class LearningZoneAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_case_pool_clicks_login_and_waits_for_return_to_a(self):
        page = FakeCasePoolSsoPage()
        messages = []

        await learning_zone_module._ensure_ctexpert_case_pool_authenticated(
            page,
            "https://www.ctexpert.cn/expert-assist-web/casePool",
            status_callback=messages.append,
        )

        self.assertTrue(page.clicked)
        self.assertTrue(page.has_token)
        self.assertTrue(any("已点击“登录”" in item for item in messages))
        self.assertTrue(any("认证完成" in item for item in messages))

    async def test_case_pool_direct_api_result_is_normalized(self):
        class FakeApiPage:
            async def evaluate(self, script):
                self.script = script
                return {
                    "values": [
                        "https://kc.zhixueyun.com/#/study/course/detail/"
                        "11111111-1111-1111-1111-111111111111",
                        "https://kc.zhixueyun.com/app/#/resource?businessType=2&"
                        "businessId=22222222-2222-2222-2222-222222222222",
                    ],
                    "records": 435,
                    "total": 435,
                    "pages": 5,
                }

        page = FakeApiPage()
        links, stats = await learning_zone_module._fetch_ctexpert_case_pool_links(page)

        self.assertIn("case/getCaseHomeList", page.script)
        self.assertEqual(len(links), 2)
        self.assertEqual(stats, {"records": 435, "total": 435, "pages": 5})


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


class FakeDirectTopicResponse:
    ok = True

    def __init__(self, url, html):
        self.url = url
        self._html = html

    async def text(self):
        return self._html


class FakeDirectTopicRequest:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class FakeDirectTopicContext(FakeLearningZoneContext):
    def __init__(self, page, url, html):
        super().__init__(page)
        self.request = FakeDirectTopicRequest(
            FakeDirectTopicResponse(url, html)
        )


class LearningZoneCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_collection_reads_topic_html_directly_before_page_navigation(self):
        topic_url = "https://cms.mylearning.cn/safe/topic/example/pc.html"
        course_url = (
            "https://kc.zhixueyun.com/#/study/course/detail/"
            "11111111-1111-1111-1111-111111111111"
        )
        page = FakeLearningZonePage()
        context = FakeDirectTopicContext(
            page,
            topic_url,
            f'<a href="{course_url}">课程</a>',
        )
        messages = []

        with patch(
            "core.learning.zone.enqueue_learning_links_with_subject_expand",
            new=AsyncMock(
                return_value={
                    "course_links": 1,
                    "subject_links": 0,
                    "course_added": 1,
                    "subject_learning_added": 0,
                    "learning_added": 1,
                    "exam_added": 0,
                }
            ),
        ) as mock_enqueue:
            added = await collect_learning_links_from_learning_zone_urls(
                [topic_url],
                context=context,
                status_callback=messages.append,
            )

        self.assertEqual(added, 1)
        self.assertFalse(hasattr(page, "wait_until"))
        self.assertEqual(context.request.calls[0][0], topic_url)
        self.assertEqual(mock_enqueue.await_args.args[0], [course_url])
        self.assertTrue(any("HTTP 直接读取" in item for item in messages))

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
