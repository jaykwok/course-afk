import unittest
from unittest.mock import AsyncMock

from core.learning import zone as learning_zone_module
from core.learning.zone import (
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
                    "complete": True,
                }

        page = FakeApiPage()
        links, stats = await learning_zone_module._fetch_ctexpert_case_pool_links(page)

        self.assertIn("case/getCaseHomeList", page.script)
        self.assertEqual(len(links), 2)
        self.assertEqual(stats, {"records": 435, "total": 435, "pages": 5, "complete": True})


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

    async def dispose(self):
        self.disposed = True


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
    async def test_manifest_merges_direct_html_and_rendered_links(self):
        topic_url = "https://cms.mylearning.cn/safe/topic/example/pc.html"
        course_url = "https://kc.zhixueyun.com/#/study/course/detail/11111111-1111-1111-1111-111111111111"
        page = FakeLearningZonePage()
        context = FakeDirectTopicContext(page, topic_url, f'<a href="{course_url}">课程</a>')
        messages = []
        result = await learning_zone_module.read_learning_zone_manifest(page, context, topic_url, messages.append)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.links), 2)
        self.assertIn(course_url, result.links)
        self.assertTrue(context.request.response.disposed)
        self.assertTrue(any("HTTP 直接读取" in item for item in messages))

    async def test_manifest_closes_overlay_and_waits_for_dynamic_links(self):
        page = FakeLearningZonePage()
        messages = []
        result = await learning_zone_module.read_learning_zone_manifest(
            page, FakeLearningZoneContext(page), "https://cms.mylearning.cn/zone.html", messages.append)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.links), 1)
        self.assertIn(500, page.waits)
        self.assertTrue(any("已关闭 1 个页面弹窗" in item for item in messages))

    async def test_topic_with_only_class_cards_is_ready_without_waiting_for_courses(self):
        page = FakeLearningZonePage()
        page.content = AsyncMock(return_value='<a href="https://kc.zhixueyun.com/app/wechat/#/qrScan?businessType=6&amp;businessId=11111111-1111-4111-8111-111111111111">班级</a>')
        result = await learning_zone_module.read_learning_zone_manifest(
            page, FakeLearningZoneContext(page), "https://cms.mylearning.cn/safe/topic/test/pc.html")
        self.assertTrue(result.complete)
        self.assertEqual(result.links, ("https://kc.zhixueyun.com/#/train-new/class-detail/11111111-1111-4111-8111-111111111111",))
        self.assertNotIn(500, page.waits)


if __name__ == "__main__":
    unittest.main()
