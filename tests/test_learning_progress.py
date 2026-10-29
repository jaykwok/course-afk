import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

from core.abort import PartialCourseFailure, SyncTimeoutError, VideoPlayerNotReadyError
from core.learning import common, flows, progress


COURSE_ID = "89fe07e2-f087-4ff9-9b81-018f45c4edd2"
SECTION_ID = "83982573-5b58-4bf1-a40f-856cce6cb0a4"
DOCUMENT_ID = "b0e9485e-b423-4837-9bb8-c40b6748fff1"
SUBJECT_ID = "11111111-1111-1111-1111-111111111111"
COURSE_URL = f"https://kc.zhixueyun.com/#/study/course/detail/{COURSE_ID}"


class Box:
    def __init__(self, section_id=SECTION_ID, section_type="6", text="必修 视频 15:00\n需再学 01:00"):
        self.section_id = section_id
        self.section_type = section_type
        self.text = text

    async def get_attribute(self, name):
        return {"id": f"D201show-sections{self.section_id}", "data-sectiontype": self.section_type}.get(name)

    def locator(self, selector):
        return SimpleNamespace(inner_text=AsyncMock(side_effect=lambda **kwargs: self.text), scroll_into_view_if_needed=AsyncMock())


def state(section_id=SECTION_ID, status=2):
    return {"section_id": section_id, "record_found": True, "finish_status": status, "completed_rate": 91}


class ServerProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.page = SimpleNamespace(url=COURSE_URL)
        self.info = {"id": COURSE_ID, "courseChapters": [{"courseChapterSections": [
            {"id": SECTION_ID, "resourceId": "video"},
            {"id": DOCUMENT_ID, "resourceId": "document"},
        ]}]}
        self.records = [{"courseId": COURSE_ID, "sectionId": SECTION_ID, "resourceId": "video", "finishStatus": 2, "completedRate": 91, "studyTotalTime": 812}]
        self.enterContext(patch.object(progress, "get_authorization_header", new=AsyncMock(return_value="test")))
        self.fetch = self.enterContext(patch.object(progress, "fetch_json", new=AsyncMock(side_effect=[self.info, self.records])))

    async def test_reads_exact_section_completion_and_missing_document_record(self):
        states = await progress.read_server_section_states(self.page)
        self.assertTrue(progress.section_completed_on_server(states[SECTION_ID]))
        self.assertFalse(progress.section_completed_on_server(states[DOCUMENT_ID]))
        self.assertFalse(states[DOCUMENT_ID]["record_found"])
        self.assertEqual(states[SECTION_ID]["completed_rate"], 91)
        request = self.fetch.await_args
        self.assertTrue(request.args[1].endswith("/course-section-progress"))
        self.assertEqual(request.kwargs["method"], "POST")
        self.assertEqual(parse_qs(request.kwargs["data"]), {"courseId": [COURSE_ID], "resourceIds": ["video,document"]})
        self.assertIn("application/x-www-form-urlencoded", request.kwargs["headers"]["Content-Type"])

    async def test_preserves_subject_context_in_course_lookup(self):
        self.page.url += f"/6/0/{SUBJECT_ID}"
        await progress.read_server_section_states(self.page)
        self.assertTrue(self.fetch.await_args_list[0].args[1].endswith(f"?type=6&sourceId={SUBJECT_ID}"))

    async def test_invalid_temporary_route_does_not_use_bare_course_permissions(self):
        self.page.url = COURSE_URL.rsplit("/", 1)[0] + "/detailInfo123"
        self.page.evaluate = AsyncMock(return_value=None)
        self.assertIsNone(await progress.read_server_section_states(self.page))
        self.fetch.assert_not_awaited()

    async def test_wrong_course_section_resource_or_duplicate_is_not_success(self):
        for changes in ({"courseId": "other"}, {"sectionId": DOCUMENT_ID}, {"resourceId": "other"}, {"sectionId": "other"}):
            with self.subTest(changes=changes):
                record = {**self.records[0], **changes}
                self.fetch.side_effect = [self.info, [record]]
                self.assertIsNone(await progress.read_server_section_states(self.page))
        self.fetch.side_effect = [self.info, [self.records[0], self.records[0]]]
        self.assertIsNone(await progress.read_server_section_states(self.page))

    async def test_invalid_payload_or_request_failure_is_unverified(self):
        for payloads in ([{"errorCode": 401}, []], [self.info, {"errorCode": 401}],
                         [{**self.info, "id": "other"}, []], [self.info, [None]],
                         [self.info, RuntimeError("network")]):
            with self.subTest(payloads=payloads):
                self.fetch.side_effect = payloads
                self.assertIsNone(await progress.read_server_section_states(self.page))

    async def test_cancellation_propagates(self):
        self.fetch.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await progress.read_server_section_states(self.page)

    async def test_dom_id_must_identify_exact_section(self):
        self.assertEqual(await progress.section_id_from_box(Box()), SECTION_ID)
        unknown = SimpleNamespace(get_attribute=AsyncMock(return_value="chapter-1"))
        self.assertIsNone(await progress.read_server_section_state(self.page, unknown))
        self.fetch.assert_not_awaited()

    def test_completion_flag_is_authoritative_not_percentage_or_study_time(self):
        self.assertTrue(progress.section_completed_on_server({"finish_status": 2, "completed_rate": 91}))
        self.assertFalse(progress.section_completed_on_server({"finish_status": 1, "completed_rate": 100, "study_total_time": 9999}))
        self.assertFalse(progress.section_completed_on_server(None))


class SyncConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.started = self.enterContext(patch.object(common, 'ensure_course_started', new=AsyncMock(return_value={})))

    async def test_stale_dom_does_not_timeout_when_server_confirms(self):
        page = SimpleNamespace(wait_for_timeout=AsyncMock())
        with patch.object(common, "read_server_section_state", new=AsyncMock(return_value=state())) as verify:
            await common.wait_until_learned(Box(), page, max_wait=0, poll_interval=1)
        verify.assert_awaited_once()
        self.started.assert_not_awaited()

    async def test_unknown_or_unfinished_server_state_keeps_failure_diagnostics(self):
        page = SimpleNamespace(wait_for_timeout=AsyncMock())
        for server_state in (None, state(status=1), {"record_found": False, "finish_status": None}):
            with self.subTest(server_state=server_state), patch.object(common, "read_server_section_state", new=AsyncMock(return_value=server_state)):
                with self.assertRaises(SyncTimeoutError) as caught:
                    await common.wait_until_learned(Box(), page, max_wait=0, poll_interval=1)
                self.assertIn("需再学", caught.exception.detail["last_progress_text"])
                self.assertEqual(caught.exception.detail["server_progress"], server_state)

    async def test_completed_dom_uses_fallback_when_server_is_unavailable(self):
        with patch.object(common, "read_server_section_state", new=AsyncMock(return_value=None)) as verify:
            await common.wait_until_learned(Box(text="必修 视频 15:00"), object(), max_wait=0, poll_interval=1)
        verify.assert_awaited_once()

    async def test_server_completion_during_poll_exits_before_five_minute_cap(self):
        now = [0.0]
        async def wait(milliseconds):
            now[0] += milliseconds / 1000
        page = SimpleNamespace(wait_for_timeout=AsyncMock(side_effect=wait))
        with patch.object(common, "monotonic", side_effect=lambda: now[0]), patch.object(common, "jitter", return_value=30), patch.object(common, "read_server_section_state", new=AsyncMock(side_effect=[state(status=1), state()])) as verify:
            await common.wait_until_learned(Box(), page, max_wait=300, poll_interval=30)
        self.assertEqual(now[0], 30)
        self.assertEqual(verify.await_count, 2)
        self.started.assert_awaited_once()

    async def test_initial_server_completion_returns_without_sleep(self):
        page = SimpleNamespace(wait_for_timeout=AsyncMock())
        with patch.object(common, "read_server_section_state", new=AsyncMock(return_value=state())):
            await common.wait_until_learned(Box(), page, max_wait=300, poll_interval=30)
        page.wait_for_timeout.assert_not_awaited()

    async def test_unfinished_server_state_overrides_stale_completed_dom(self):
        now = [0.0]
        async def wait(milliseconds):
            now[0] += milliseconds / 1000
        page = SimpleNamespace(wait_for_timeout=AsyncMock(side_effect=wait))
        with patch.object(common, "monotonic", side_effect=lambda: now[0]), patch.object(common, "jitter", return_value=30), patch.object(common, "read_server_section_state", new=AsyncMock(return_value=state(status=1))) as verify:
            with self.assertRaises(SyncTimeoutError):
                await common.wait_until_learned(Box(text="必修 视频 15:00"), page, max_wait=300, poll_interval=30)
        self.assertEqual(now[0], 300)
        self.assertGreater(verify.await_count, 1)

    async def test_api_latency_counts_towards_total_budget(self):
        now = [0.0]
        async def wait(milliseconds):
            now[0] += milliseconds / 1000
        async def verify(*args):
            now[0] += 10
            return state(status=1)
        page = SimpleNamespace(wait_for_timeout=AsyncMock(side_effect=wait))
        with patch.object(common, "monotonic", side_effect=lambda: now[0]), patch.object(common, "jitter", return_value=30), patch.object(common, "read_server_section_state", new=verify):
            with self.assertRaises(SyncTimeoutError):
                await common.wait_until_learned(Box(), page, max_wait=90, poll_interval=30)
        self.assertEqual(sum(call.args[0] for call in page.wait_for_timeout.await_args_list), 60000)

    async def test_slow_api_does_not_hold_confirmation_past_deadline(self):
        async def slow(*args):
            await asyncio.sleep(10)
        with patch.object(common, "read_section_progress_text", new=AsyncMock(return_value="需再学 00:08")), patch.object(common, "read_server_section_state", new=slow):
            with self.assertRaises(SyncTimeoutError):
                await asyncio.wait_for(common.wait_until_learned(Box(), object(), max_wait=1, poll_interval=1), timeout=2)


class CourseFinalRecheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.boxes = [Box(), Box(DOCUMENT_ID, "1", "必修 文档 05:00\n需学 00:05")]
        self.page = SimpleNamespace(url=COURSE_URL, wait_for_load_state=AsyncMock())
        for name in ("handle_rating_popup", "handle_archive_continue_popup", "ensure_course_page_ready", "_ensure_course_learning_view", "_activate_course_section"):
            self.enterContext(patch.object(flows, name, new=AsyncMock(return_value=False)))
        self.enterContext(patch.object(flows, "_is_course_completed", new=AsyncMock(return_value=False)))
        self.enterContext(patch.object(flows, "_collect_chapter_boxes", new=AsyncMock(return_value=[("required", box) for box in self.boxes])))
        self.enterContext(patch.object(flows, "queue_course_exams_from_api", new=AsyncMock(return_value=None)))
        self.enterContext(patch.object(flows, "handle_video", new=AsyncMock(side_effect=SyncTimeoutError(detail={"last_progress_text": self.boxes[0].text}))))
        self.record = self.enterContext(patch.object(flows, "record_learning_failure"))

        async def finish_document(*args):
            self.boxes[1].text = "必修 文档 05:00"
        self.enterContext(patch.object(flows, "handle_document", new=finish_document))
        self.verify = self.enterContext(patch.object(flows, "read_server_section_states", new=AsyncMock(return_value={SECTION_ID: state(), DOCUMENT_ID: state(DOCUMENT_ID)})))

    async def test_late_server_completion_clears_only_sync_failure_despite_stale_dom(self):
        self.assertTrue(await flows.course_learning(self.page))
        self.verify.assert_awaited_once()
        self.record.assert_not_called()

    async def test_missing_document_completion_is_retained_with_section_identity(self):
        self.boxes[0].section_type = "1"
        self.boxes[0].text = "必修 文档 05:00\n需学 00:05"
        self.boxes[1].text = "必修 视频 15:00"
        self.verify.return_value[SECTION_ID] = {"record_found": False, "finish_status": None}
        with patch.object(flows, "handle_document", new=AsyncMock(side_effect=SyncTimeoutError())):
            with self.assertRaises(PartialCourseFailure) as caught:
                await flows.course_learning(self.page)
        failure = caught.exception.detail["chapter_failures"][0]
        self.assertEqual(failure["section_type"], "1")
        self.assertFalse(failure["detail"]["server_progress_at_course_end"]["record_found"])

    async def test_failed_api_recheck_keeps_original_timeout(self):
        self.verify.return_value = None
        with self.assertRaises(PartialCourseFailure) as caught:
            await flows.course_learning(self.page)
        self.assertEqual(caught.exception.detail["chapter_failures"][0]["reason"], "sync_timeout")

    async def test_completed_server_state_does_not_hide_player_errors(self):
        with patch.object(flows, "handle_video", new=AsyncMock(side_effect=VideoPlayerNotReadyError())):
            with self.assertRaises(PartialCourseFailure) as caught:
                await flows.course_learning(self.page)
        self.assertEqual(caught.exception.detail["chapter_failures"][0]["reason"], "video_player_not_ready")
        self.verify.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
