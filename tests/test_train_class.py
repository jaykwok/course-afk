import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from core.discovery.train_class import (
    build_activity_list_url,
    collect_learning_links_from_train_class_urls,
    extract_class_id,
    extract_learning_links_from_activity_items,
    map_activity_to_learning_url,
)


class TrainClassMappingTests(unittest.TestCase):
    def test_extract_class_id_from_detail_and_paas(self):
        class_id = "e8d1e9b6-f9cf-4960-bf0f-57cd34dc0ca9"
        self.assertEqual(
            extract_class_id(
                f"https://kc.zhixueyun.com/#/train-new/class-detail/{class_id}"
            ),
            class_id,
        )
        self.assertEqual(
            extract_class_id(
                "https://kc.zhixueyun.com/#/paas-container?paasurl="
                f"website%2Fx%2Fdefault%3FclassId%3D{class_id}"
            ),
            class_id,
        )
        self.assertEqual(
            extract_class_id(
                "https://kc.zhixueyun.com/app/wechat/#/qrScan?"
                f"businessType=6&businessId={class_id}"
            ),
            class_id,
        )

    def test_build_activity_list_url_first_page_and_more(self):
        first = build_activity_list_url(
            class_id="class-1",
            chapter_id="chapter-1",
            page=1,
            page_size=5,
        )
        more = build_activity_list_url(
            class_id="class-1",
            chapter_id="chapter-1",
            page=2,
            page_size=5,
        )
        self.assertIn("/chapter-activity-list/paas?", first)
        self.assertNotIn("/more", first)
        self.assertIn("/chapter-activity-list/paas/more?", more)
        self.assertIn("page=1", first)
        self.assertIn("page=2", more)
        self.assertIn("pageSize=5", first)
        self.assertIn("pageSize=5", more)

    def test_map_type11_business_value_to_subject(self):
        url = map_activity_to_learning_url(
            {
                "businessType": 11,
                "businessId": "92c90a5e-cc8c-4c6a-97bd-3f2c93815e9b",
                "businessValue": (
                    "https://kc.zhixueyun.com/app/wechat/#/qrScan?"
                    "businessType=2&businessId="
                    "1d40d4e0-a622-4535-8f02-ad108a930656"
                ),
            }
        )
        self.assertEqual(
            url,
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "1d40d4e0-a622-4535-8f02-ad108a930656",
        )

    def test_map_type1_and_type2_by_business_id(self):
        self.assertEqual(
            map_activity_to_learning_url(
                {
                    "businessType": 1,
                    "businessId": "11111111-1111-1111-1111-111111111111",
                    "businessValue": "",
                }
            ),
            "https://kc.zhixueyun.com/#/study/course/detail/"
            "11111111-1111-1111-1111-111111111111",
        )
        self.assertEqual(
            map_activity_to_learning_url(
                {
                    "businessType": "2",
                    "businessId": "22222222-2222-2222-2222-222222222222",
                }
            ),
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "22222222-2222-2222-2222-222222222222",
        )

    def test_map_type8_course_by_business_id(self):
        # 实勘：UI 前缀「课程」，type=8 + businessId
        self.assertEqual(
            map_activity_to_learning_url(
                {
                    "businessType": 8,
                    "businessId": "f8f48345-5a1a-48f7-a5e8-7b8bd09b7d04",
                    "businessValue": "",
                    "businessName": "推动战略升级…柯瑞文",
                }
            ),
            "https://kc.zhixueyun.com/#/study/course/detail/"
            "f8f48345-5a1a-48f7-a5e8-7b8bd09b7d04",
        )

    def test_extract_learning_links_from_activity_items(self):
        links = extract_learning_links_from_activity_items(
            [
                {
                    "businessType": 11,
                    "businessValue": (
                        "https://kc.zhixueyun.com/app/wechat/#/qrScan?"
                        "businessType=2&businessId="
                        "1d40d4e0-a622-4535-8f02-ad108a930656"
                    ),
                },
                {
                    "businessType": 8,
                    "businessValue": "",
                    "businessId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                },
                {
                    "businessType": 1,
                    "businessId": "11111111-1111-1111-1111-111111111111",
                },
            ]
        )
        self.assertEqual(
            links,
            [
                "https://kc.zhixueyun.com/#/study/subject/detail/"
                "1d40d4e0-a622-4535-8f02-ad108a930656",
                "https://kc.zhixueyun.com/#/study/course/detail/"
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "https://kc.zhixueyun.com/#/study/course/detail/"
                "11111111-1111-1111-1111-111111111111",
            ],
        )

class FakeApiResponse:
    def __init__(self, payload, *, ok=True, status=200):
        self._payload = payload
        self.ok = ok
        self.status = status

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)


def _activity(name: str, business_type: int, business_id: str, value: str = ""):
    return {
        "id": business_id,
        "businessName": name,
        "businessType": business_type,
        "businessId": business_id,
        "businessValue": value,
    }


class FakeRequestApi:
    """模拟实勘分页：page1=5 条，page2(more)=5 条，page3(more)=3 条。"""

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []
        self._pages = {
            1: [
                _activity(
                    "专题",
                    11,
                    "92c90a5e-cc8c-4c6a-97bd-3f2c93815e9b",
                    (
                        "https://kc.zhixueyun.com/app/wechat/#/qrScan?"
                        "businessType=2&businessId="
                        "1d40d4e0-a622-4535-8f02-ad108a930656"
                    ),
                ),
                _activity("课1", 8, "11111111-1111-1111-1111-111111111101"),
                _activity("课2", 8, "11111111-1111-1111-1111-111111111102"),
                _activity("课3", 8, "11111111-1111-1111-1111-111111111103"),
                _activity("课4", 8, "11111111-1111-1111-1111-111111111104"),
            ],
            2: [
                _activity("课5", 8, "11111111-1111-1111-1111-111111111105"),
                _activity("课6", 8, "11111111-1111-1111-1111-111111111106"),
                _activity("课7", 8, "11111111-1111-1111-1111-111111111107"),
                _activity("课8", 8, "11111111-1111-1111-1111-111111111108"),
                _activity("课9", 8, "11111111-1111-1111-1111-111111111109"),
            ],
            3: [
                _activity("课10", 8, "11111111-1111-1111-1111-111111111110"),
                _activity("课11", 8, "11111111-1111-1111-1111-111111111111"),
                _activity("课12", 8, "11111111-1111-1111-1111-111111111112"),
            ],
        }

    async def get(self, url, headers=None):
        self.calls.append((url, headers))
        if "chapter/paas" in url and "activity" not in url:
            return FakeApiResponse(
                [{"id": "4d06a78e-0cb2-46d1-8ac3-f672979a6a5b", "name": "阶段"}]
            )
        if "chapter-activity-list" in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            page = int((params.get("page") or ["1"])[0])
            is_more = "/more" in parsed.path
            # 实勘：page1 非 more；page>=2 必须 more
            if page == 1 and is_more:
                return FakeApiResponse({"items": [], "recordCount": 0})
            if page >= 2 and not is_more:
                return FakeApiResponse({"items": [], "recordCount": 0})
            items = self._pages.get(page, [])
            # 首页 recordCount 故意写 5（不可信），与实勘一致
            return FakeApiResponse(
                {"items": items, "recordCount": 5 if page == 1 else 0}
            )
        return FakeApiResponse({"message": "missing"}, ok=False, status=404)


class FakeTrainClassPage:
    def __init__(self):
        self.closed = False
        self.goto_urls = []
        self.context = type("Ctx", (), {"request": FakeRequestApi()})()

    async def goto(self, url, wait_until=None):
        self.goto_urls.append(url)
        self.wait_until = wait_until

    async def evaluate(self, _script):
        return "Bearer__test-token"

    async def wait_for_timeout(self, _milliseconds):
        return None

    async def close(self):
        self.closed = True


class FakeTrainClassContext:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


class TrainClassCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_collection_paginates_with_more_endpoint(self):
        page = FakeTrainClassPage()
        context = FakeTrainClassContext(page)
        messages = []
        lifecycle = {"exited": False, "callback": None}
        captured_links: list[str] = []

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
            # 5+5+3=13 活动：12 课程 + 1 主题展开 1 条 → learning_added=13
            course_n = sum(1 for u in learning_links if "/course/detail/" in u)
            subject_n = sum(1 for u in learning_links if "/subject/detail/" in u)
            captured_links.extend(
                [u for u in learning_links if "/course/detail/" in u]
            )
            return {
                "course_links": course_n,
                "subject_links": subject_n,
                "course_added": course_n,
                "subject_learning_added": subject_n,
                "learning_added": course_n + subject_n,
                "exam_added": 0,
            }

        with (
            patch(
                "core.discovery.train_class.create_browser_context",
                side_effect=fake_browser_context,
            ),
            patch(
                "core.discovery.train_class.prepare_page_after_navigation_async",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "core.discovery.train_class.wait_for_authorization_header",
                new=AsyncMock(return_value="Bearer__test-token"),
            ),
            patch(
                "core.discovery.train_class.enqueue_learning_links_with_subject_expand",
                side_effect=fake_enqueue,
            ),
        ):
            added = await collect_learning_links_from_train_class_urls(
                [
                    "https://kc.zhixueyun.com/#/train-new/class-detail/"
                    "e8d1e9b6-f9cf-4960-bf0f-57cd34dc0ca9"
                ],
                status_callback=messages.append,
                before_close_callback=lambda count: lifecycle.update(
                    callback=(count, lifecycle["exited"])
                ),
            )

        # 5 + 5 + 3 = 13 条活动：12 课程直接入队 + 1 主题走展开
        self.assertEqual(added, 13)
        self.assertEqual(len(captured_links), 12)
        self.assertEqual(len(enqueue_calls), 1)
        self.assertIn(
            "https://kc.zhixueyun.com/#/study/subject/detail/"
            "1d40d4e0-a622-4535-8f02-ad108a930656",
            enqueue_calls[0],
        )
        self.assertTrue(page.closed)

        activity_calls = [
            url
            for url, _ in page.context.request.calls
            if "chapter-activity-list" in url
        ]
        self.assertEqual(len(activity_calls), 3)
        self.assertIn("/paas?", activity_calls[0])
        self.assertNotIn("/more", activity_calls[0])
        self.assertIn("/paas/more?", activity_calls[1])
        self.assertIn("page=2", activity_calls[1])
        self.assertIn("/paas/more?", activity_calls[2])
        self.assertIn("page=3", activity_calls[2])

        # 不因错误的 recordCount=5 提前结束
        self.assertTrue(
            any("12 条课程、1 个主题" in item for item in messages)
        )
        self.assertTrue(
            all(
                link.startswith(
                    "https://kc.zhixueyun.com/#/study/course/detail/"
                )
                for link in captured_links
            )
        )
        self.assertEqual(lifecycle["callback"], (13, False))
        self.assertTrue(lifecycle["exited"])


if __name__ == "__main__":
    unittest.main()
