import asyncio
from contextlib import asynccontextmanager, ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from core.abort import BrowserCertificateError, UserCancelRequested
from core.exam import runner
from core.exam.answers import ExamAiConfigurationError
from core.exam.flow import ExamQuestionExtractionError
from core.exam.entry_api import ExamEntry, ExamEntryUnavailable
from core.learning.exam_api import CourseExamState
from core.queues.exam import append_exam_urls, read_exam_urls, remove_exam_url, has_ai_failed_model_config, record_ai_failed_model_config
from core.queues.history import pending_submission, record_submission_intent
from core.queues.manual_exam import append_manual_exam_entry, read_manual_exam_queue

COURSE = "https://kc.zhixueyun.com/#/study/course/detail/00000000-0000-0000-0000-000000000001"
PAPER = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/00000000-0000-0000-0000-000000000002"
MODEL = {"model": "test", "request_type": "responses", "account": "account-a"}
NEW = CourseExamState(False, False, 5, 0, 5)


class Context:
    def __init__(self):
        self.pages = []
        self.closed = False

    async def new_page(self):
        page = AsyncMock()
        self.pages.append(page)
        return page

    @asynccontextmanager
    async def open(self):
        try:
            yield None, self
        finally:
            self.closed = True


class TransactionFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(TemporaryDirectory()))
        self.exam_file = self.tmp / "exam.json"
        self.manual_file = self.tmp / "manual.json"
        self.client = AsyncMock()
        self.context = Context()
        for name, value in (("EXAM_URLS_FILE", self.exam_file), ("MANUAL_EXAM_FILE", self.manual_file)):
            self.stack.enter_context(patch.object(runner, name, value))
        self.stack.enter_context(patch.object(runner, "_build_exam_client", return_value=(self.client, "test")))
        self.stack.enter_context(patch.object(runner, "_build_ai_exam_model_config", return_value=MODEL))
        self.stack.enter_context(patch.object(runner, "_current_account", return_value=MODEL["account"]))
        self.stack.enter_context(patch.object(runner, "create_browser_context", self.context.open))

    def enqueue(self, urls=(COURSE, PAPER)):
        append_exam_urls(list(urls), file_path=self.exam_file)

    def manual(self):
        return read_manual_exam_queue(self.manual_file)

    def pending(self, url=PAPER, account="account-a"):
        return pending_submission(url, self.exam_file, account=account)


class TransactionTests(TransactionFixture):
    async def test_exam_detail_entry_uses_paper_runner_and_preserves_url(self):
        url = (
            "https://kc.zhixueyun.com/#/exam/exam/exam-detail/"
            "9ace2356-f9a1-4ad1-a0c2-5c4a1d4e6a92"
        )
        self.enqueue((url,))
        with patch.object(runner, "_run_paper_ai_exam", new=AsyncMock(return_value=True)) as exam:
            await runner.run_ai_exam_batch()
        exam.assert_awaited_once_with(self.context.pages[0], url, self.client, "test", auto_submit=False)
        self.context.pages[0].goto.assert_awaited_once_with(url, timeout=30000)
        self.assertEqual(read_exam_urls(self.exam_file), [])
        self.assertEqual(self.manual(), [])

    async def test_paper_state_extracts_exam_id_from_both_entry_routes(self):
        exam_id = "9ace2356-f9a1-4ad1-a0c2-5c4a1d4e6a92"
        page = object()
        for route in ("answer-paper", "exam-detail"):
            with self.subTest(route=route):
                url = f"https://kc.zhixueyun.com/#/exam/exam/{route}/{exam_id}?from=manual"
                with patch.object(runner, "read_exam_state", new=AsyncMock(return_value=NEW)) as read_state:
                    self.assertIs(await runner._paper_state(page, url), NEW)
                read_state.assert_awaited_once_with(page, exam_id)

    async def test_certificate_failure_does_not_remove_or_manually_route_exam(self):
        self.enqueue()
        with patch.object(runner, "_run_course_ai_exam", new=AsyncMock(side_effect=RuntimeError("net::ERR_CERT_AUTHORITY_INVALID"))):
            with self.assertRaises(BrowserCertificateError):
                await runner.run_ai_exam_batch()
        self.assertEqual(read_exam_urls(self.exam_file), [COURSE, PAPER])
        self.assertEqual(self.manual(), [])
        self.client.close.assert_awaited_once()

    async def test_verified_item_is_checkpointed_before_next_item(self):
        self.enqueue()
        async def second(*_args, **_kwargs):
            self.assertEqual(read_exam_urls(self.exam_file), [PAPER])
            raise UserCancelRequested("stop")
        with patch.object(runner, "_run_course_ai_exam", new=AsyncMock(return_value=True)), patch.object(runner, "_run_paper_ai_exam", new=AsyncMock(side_effect=second)):
            with self.assertRaises(UserCancelRequested):
                await runner.run_ai_exam_batch()
        self.assertEqual(read_exam_urls(self.exam_file), [PAPER])
        self.client.close.assert_awaited_once()
        self.assertTrue(self.context.closed)
        for page in self.context.pages:
            page.close.assert_awaited_once()

    async def test_cancel_close_extract_and_config_failures_keep_current_and_remaining(self):
        class TargetClosedError(Exception):
            pass
        errors = [asyncio.CancelledError(), TargetClosedError(), ExamQuestionExtractionError("missing"), ExamAiConfigurationError("bad config")]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.enqueue()
                with patch.object(runner, "_run_course_ai_exam", new=AsyncMock(side_effect=error)):
                    with self.assertRaises((UserCancelRequested, ExamAiConfigurationError)):
                        await runner.run_ai_exam_batch()
                self.assertEqual(read_exam_urls(self.exam_file), [COURSE, PAPER])
                self.assertEqual(self.manual(), [])

    async def test_manual_destination_write_failure_preserves_ai_source(self):
        self.enqueue(("https://kc.zhixueyun.com/#/study/subject/detail/subject",))
        with patch.object(runner, "append_manual_exam_entry", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await runner.run_ai_exam_batch()
        self.assertEqual(len(read_exam_urls(self.exam_file)), 1)
        self.client.close.assert_awaited_once()

    async def test_source_removal_failure_leaves_a_recoverable_duplicate(self):
        url = "https://kc.zhixueyun.com/#/study/subject/detail/subject"
        self.enqueue((url,))
        with patch.object(runner, "remove_exam_url", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await runner.run_ai_exam_batch()
        self.assertEqual(read_exam_urls(self.exam_file), [url])
        self.assertEqual([item.url for item in self.manual()], [url])

    async def test_model_failure_survives_queue_removal_and_reimport(self):
        self.enqueue((COURSE,))
        record_ai_failed_model_config(COURSE, MODEL, file_path=self.exam_file)
        remove_exam_url(COURSE, file_path=self.exam_file)
        self.enqueue((COURSE,))
        with patch.object(runner, "_run_course_ai_exam", new=AsyncMock(return_value=True)) as exam:
            await runner.run_ai_exam_batch(auto_submit=True)
        exam.assert_awaited_once()
        self.assertFalse(exam.call_args.kwargs["auto_submit"])
        self.assertEqual(read_exam_urls(self.exam_file), [])
        self.assertEqual(self.manual(), [])
        self.assertTrue(has_ai_failed_model_config(COURSE, MODEL, file_path=self.exam_file))
        self.assertFalse(has_ai_failed_model_config(COURSE, {**MODEL, "account": "account-b"}, file_path=self.exam_file))



class PaperReconciliationTests(TransactionFixture):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(runner, "_raise_if_login_required", new=AsyncMock()))
        self.stack.enter_context(patch.object(runner, "_handle_attempt_limit_if_present", new=AsyncMock(return_value=False)))
        self.stack.enter_context(patch.object(runner, "_has_ready_answer_question", new=AsyncMock(return_value=True)))
        self.answer = self.stack.enter_context(patch.object(runner, "ai_exam", new=AsyncMock()))

    async def run_paper(self, before=NEW, after=None):
        with patch.object(runner, "_paper_state", new=AsyncMock(side_effect=[before, after])):
            return await runner._run_paper_ai_exam(object(), PAPER, self.client, "test")

    async def test_threshold_exception_only_disables_auto_submit_for_its_own_url(self):
        normal = PAPER.rsplit("/", 1)[0] + "/00000000-0000-0000-0000-000000000003"
        self.enqueue((PAPER, normal))
        with patch.object(runner, "_paper_state", new=AsyncMock(side_effect=[replace(NEW, remaining_attempts=1), replace(NEW, passed=True), NEW, replace(NEW, passed=True)])):
            await runner.run_ai_exam_batch(auto_submit=True)
        self.assertEqual([call.kwargs["auto_submit"] for call in self.answer.await_args_list], [False, True])
        self.assertEqual([call.args[3] for call in self.answer.await_args_list], [PAPER, normal])

    async def test_unverified_resume_only_disables_auto_submit_for_its_own_url(self):
        normal = PAPER.rsplit("/", 1)[0] + "/00000000-0000-0000-0000-000000000003"
        self.enqueue((PAPER, normal))
        record_submission_intent(PAPER, MODEL, self.exam_file)
        with patch.object(runner, "_paper_state", new=AsyncMock(side_effect=[NEW, replace(NEW, passed=True), NEW, replace(NEW, passed=True)])):
            await runner.run_ai_exam_batch(auto_submit=True)
        self.assertEqual([call.kwargs["auto_submit"] for call in self.answer.await_args_list], [False, True])

    async def test_intent_is_durable_before_answering_and_verified_afterward(self):
        async def answer(*_args, **_kwargs):
            self.assertIsNotNone(self.pending())
        self.answer.side_effect = answer
        self.assertTrue(await self.run_paper(after=replace(NEW, passed=True)))
        self.answer.assert_awaited_once()
        self.assertIsNone(self.pending())

    async def test_unverified_previous_submission_resumes_without_auto_submit(self):
        record_submission_intent(PAPER, MODEL, self.exam_file)
        previous = self.pending()
        self.assertTrue(await self.run_paper())
        self.answer.assert_awaited_once()
        self.assertFalse(self.answer.call_args.kwargs["auto_submit"])
        self.assertTrue(self.answer.call_args.kwargs["resume"])
        self.assertEqual(self.manual()[0].reason, "submission_unverified")
        self.assertEqual(self.pending(), previous)

    async def test_last_attempt_threshold_and_unlimited_only_control_auto_submission(self):
        for allowed, remaining, expected in ((1, 1, False), (3, 1, False), (10, 3, False), (10, 4, True), (0, None, True)):
            with self.subTest(allowed=allowed, remaining=remaining):
                before = replace(NEW, allowed_attempts=allowed, remaining_attempts=remaining)
                after = replace(NEW, passed=True)
                self.answer.reset_mock()
                with patch.object(runner, "PAPER_EXAM_ATTEMPT_THRESHOLD", 3), patch.object(runner, "_paper_state", new=AsyncMock(side_effect=[before, after])):
                    await runner._run_paper_ai_exam(object(), PAPER, self.client, "test", auto_submit=True)
                self.answer.assert_awaited_once()
                self.assertIs(self.answer.call_args.kwargs["auto_submit"], expected)

    async def test_old_unverified_history_without_api_resume_never_opens_exam(self):
        record_submission_intent(PAPER, MODEL, self.exam_file)
        entry = ExamEntry(PAPER.rsplit("/", 1)[-1], PAPER, NEW, False)
        with patch.object(runner, "_has_ready_answer_question", new=AsyncMock(return_value=False)), patch.object(runner, "read_exam_entry", new=AsyncMock(return_value=entry)), patch.object(runner, "validate_exam_entry", new=AsyncMock()) as validate:
            await self.run_paper()
        validate.assert_not_awaited()
        self.answer.assert_not_awaited()
        self.assertIsNotNone(self.pending())

    async def test_old_unverified_history_can_resume_using_api_record(self):
        record_submission_intent(PAPER, MODEL, self.exam_file)
        page = AsyncMock()
        entry = ExamEntry(PAPER.rsplit("/", 1)[-1], PAPER, NEW, True)
        with patch.object(runner, "_has_ready_answer_question", new=AsyncMock(return_value=False)), patch.object(runner, "read_exam_entry", new=AsyncMock(return_value=entry)), patch.object(runner, "validate_exam_entry", new=AsyncMock()) as validate, patch.object(runner, "_paper_state", new=AsyncMock(return_value=replace(NEW, passed=True))):
            await runner._run_paper_ai_exam(page, PAPER, self.client, "test")
        validate.assert_awaited_once_with(page, entry)
        page.goto.assert_awaited_once_with(PAPER, timeout=30000)
        self.answer.assert_awaited_once()
        self.assertFalse(self.answer.call_args.kwargs["auto_submit"])
        self.assertTrue(self.answer.call_args.kwargs["resume"])

    async def test_verified_pass_removes_stale_no_answer_review_without_answering(self):
        append_manual_exam_entry(PAPER, reason="ai_no_answer", reason_text="old", file_path=self.manual_file)
        await self.run_paper(before=replace(NEW, passed=True))
        self.answer.assert_not_awaited()
        self.assertEqual(self.manual(), [])

    async def test_migrated_queue_protection_overrides_user_auto_submit_opt_in(self):
        append_exam_urls([PAPER], file_path=self.exam_file, requires_manual_submit=True)
        with patch.object(runner, "_paper_state", new=AsyncMock(side_effect=[NEW, replace(NEW, passed=True)])):
            await runner._run_paper_ai_exam(object(), PAPER, self.client, "test", auto_submit=True)
        self.answer.assert_awaited_once()
        self.assertFalse(self.answer.call_args.kwargs["auto_submit"])

    async def test_submission_history_is_isolated_by_account(self):
        record_submission_intent(PAPER, {**MODEL, "account": "account-b"}, self.exam_file)
        await self.run_paper(after=replace(NEW, passed=True))
        self.answer.assert_awaited_once()
        self.assertIsNotNone(self.pending(account="account-b"))
        self.assertIsNone(self.pending())

    async def test_old_failure_cannot_be_attributed_to_this_attempt(self):
        failed = replace(NEW, submitted=True, record_id="old", used_attempts=1)
        await self.run_paper(before=failed, after=failed)
        self.assertEqual(self.manual()[0].reason, "submission_unverified")
        self.assertFalse(has_ai_failed_model_config(PAPER, MODEL, file_path=self.exam_file))
        self.assertIsNotNone(self.pending())

    async def test_new_failed_record_is_archived_without_automatic_retry(self):
        await self.run_paper(after=replace(NEW, submitted=True, record_id="new", used_attempts=1))
        self.answer.assert_awaited_once()
        self.assertEqual(self.manual()[0].reason, "ai_failed")
        self.assertTrue(has_ai_failed_model_config(PAPER, MODEL, file_path=self.exam_file))
        self.assertIsNone(self.pending())

    async def test_pending_grading_is_kept_for_manual_review(self):
        await self.run_paper(after=replace(NEW, pending_grading=True, submitted=True))
        self.assertEqual(self.manual()[0].reason, "pending_grading")
        self.assertFalse(has_ai_failed_model_config(PAPER, MODEL, file_path=self.exam_file))

    async def test_state_read_failure_allows_ai_but_never_auto_submits(self):
        await self.run_paper(before=None)
        self.answer.assert_awaited_once()
        self.assertFalse(self.answer.call_args.kwargs["auto_submit"])

    async def test_state_read_failure_after_exam_remains_unverified(self):
        await self.run_paper(after=None)
        self.assertEqual(self.manual()[0].reason, "submission_unverified")
        self.assertIsNotNone(self.pending())

    async def test_unknown_attempt_count_allows_ai_but_never_auto_submits(self):
        await self.run_paper(before=replace(NEW, allowed_attempts=None, used_attempts=None, remaining_attempts=None))
        self.answer.assert_awaited_once()
        self.assertFalse(self.answer.call_args.kwargs["auto_submit"])
        self.assertEqual(len(self.manual()), 1)

    async def test_auth_deadline_includes_slow_inner_operations(self):
        page = AsyncMock()
        page.url = PAPER
        async def slow(_page):
            await asyncio.sleep(10)
        with patch.object(runner, "_has_authorization_cookie", new=AsyncMock(return_value=True)), patch.object(runner, "_is_paper_entry_ready", new=slow):
            result = await asyncio.wait_for(runner._wait_for_target_route_after_auth(page, PAPER, timeout_ms=20), timeout=0.5)
        self.assertFalse(result)


class PaperApiEntryTests(TransactionFixture):
    def setUp(self):
        super().setUp()
        self.page = AsyncMock()
        self.url = PAPER.replace("/answer-paper/", "/exam-detail/")
        self.entry = ExamEntry(PAPER.rsplit("/", 1)[-1], PAPER, NEW, False)
        self.stack.enter_context(patch.object(runner, "_raise_if_login_required", new=AsyncMock()))
        self.stack.enter_context(patch.object(runner, "_has_ready_answer_question", new=AsyncMock(return_value=False)))
        self.read_entry = self.stack.enter_context(patch.object(runner, "read_exam_entry", new=AsyncMock(return_value=self.entry)))
        self.validate = self.stack.enter_context(patch.object(runner, "validate_exam_entry", new=AsyncMock()))
        self.stack.enter_context(patch.object(runner, "_paper_state", new=AsyncMock(return_value=replace(NEW, passed=True))))
        self.answer = self.stack.enter_context(patch.object(runner, "ai_exam", new=AsyncMock()))
        self.button = self.stack.enter_context(patch.object(runner, "_locate_exam_button", new=AsyncMock(side_effect=AssertionError("API entry must not inspect buttons"))))

    async def test_api_entry_records_intent_before_navigation_and_ignores_buttons(self):
        async def navigate(*_args, **_kwargs):
            self.assertIsNotNone(self.pending(self.url))
        self.page.goto.side_effect = navigate
        await runner._run_paper_ai_exam(self.page, self.url, self.client, "test", auto_submit=True)
        self.read_entry.assert_awaited_once_with(self.page, self.url)
        self.validate.assert_awaited_once_with(self.page, self.entry)
        self.page.goto.assert_awaited_once_with(PAPER, timeout=30000)
        self.answer.assert_awaited_once()
        self.assertFalse(self.answer.call_args.kwargs["resume"])
        self.button.assert_not_awaited()
        self.page.locator.assert_not_called()
        self.assertIsNone(self.pending(self.url))

    async def test_api_resume_without_local_history_preserves_answers_and_manual_submission(self):
        self.read_entry.return_value = replace(self.entry, can_resume=True)
        await runner._run_paper_ai_exam(self.page, self.url, self.client, "test", auto_submit=True)
        self.assertTrue(self.answer.call_args.kwargs["resume"])
        self.assertFalse(self.answer.call_args.kwargs["auto_submit"])
        self.button.assert_not_awaited()

    async def test_api_denial_never_opens_paper_or_records_a_new_attempt(self):
        self.validate.side_effect = ExamEntryUnavailable("attempt_limit", "次数已用完")
        await runner._run_paper_ai_exam(self.page, self.url, self.client, "test")
        self.page.goto.assert_not_awaited()
        self.answer.assert_not_awaited()
        self.assertIsNone(self.pending(self.url))
        self.assertEqual(self.manual()[0].reason, "attempt_limit")

    async def test_malformed_entry_never_falls_back_to_button_text(self):
        self.read_entry.side_effect = ExamEntryUnavailable("state_unknown", "响应不完整")
        await runner._run_paper_ai_exam(self.page, self.url, self.client, "test")
        self.validate.assert_not_awaited()
        self.page.goto.assert_not_awaited()
        self.button.assert_not_awaited()
        self.assertEqual(self.manual()[0].reason, "state_unknown")


class CourseSubmissionPolicyTests(TransactionFixture):
    async def test_course_last_attempt_is_ai_answered_with_manual_submission(self):
        for button_text, threshold, expected in (("开始考试 剩余1次", 1, False), ("开始考试 剩余2次", 2, False), ("开始考试 剩余3次", 2, True)):
            with self.subTest(button_text=button_text):
                button = SimpleNamespace(count=AsyncMock(return_value=1), inner_text=AsyncMock(return_value=button_text))
                page = SimpleNamespace(locator=lambda _selector: button)
                with patch.object(runner, "_open_course_exam_tab", new=AsyncMock()), patch.object(runner, "get_course_exam_outcome", new=AsyncMock(side_effect=["unknown", "passed"])), patch.object(runner, "_handle_exam_result", new=AsyncMock()), patch.object(runner, "wait_for_finish_test", new=AsyncMock()) as answer, patch.object(runner, "COURSE_EXAM_ATTEMPT_THRESHOLD", threshold):
                    await runner._run_course_ai_exam(page, COURSE, self.client, "test", auto_submit=True)
                answer.assert_awaited_once()
                self.assertIs(answer.call_args.kwargs["auto_submit"], expected)



if __name__ == "__main__":
    unittest.main()
