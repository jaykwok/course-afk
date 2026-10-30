import unittest
from unittest.mock import AsyncMock, patch

from core.exam.entry_api import (
    ExamEntryUnavailable, parse_exam_entry, read_exam_entry, validate_exam_entry,
)


EXAM_ID = "9ace2356-f9a1-4ad1-a0c2-5c4a1d4e6a92"
DETAIL_URL = f"https://kc.zhixueyun.com/#/exam/exam/exam-detail/{EXAM_ID}"
ANSWER_URL = f"https://kc.zhixueyun.com/#/exam/exam/answer-paper/{EXAM_ID}"


def payload():
    return {
        "id": EXAM_ID, "type": 1, "status": 5, "clientType": 0,
        "allowExamTimes": 3, "examedTimes": 0, "passScore": 60,
        "startTime": 1000, "endTime": 3000,
        "isSetAccessSecret": 0, "isSetPersonalCode": 0,
        "needApplicant": 0, "paperClass": {"totalScore": 10000},
        "examRecord": {"id": None, "status": None, "currentTime": 2000},
    }


class ExamEntryParsingTests(unittest.TestCase):
    def test_api_id_resolves_answer_route_without_button_text(self):
        entry = parse_exam_entry(payload(), EXAM_ID)
        self.assertEqual(entry.answer_url, ANSWER_URL)
        self.assertEqual(entry.state.remaining_attempts, 3)
        self.assertFalse(entry.can_resume)
        self.assertEqual(entry.blocked_reason, "")

    def test_active_record_allows_resume_even_with_no_remaining_attempts(self):
        data = payload()
        data["examedTimes"] = 3
        data["examRecord"].update(id="record", status=2, startTime=1500, isFinished=0)
        entry = parse_exam_entry(data, EXAM_ID)
        self.assertTrue(entry.can_resume)
        self.assertEqual(entry.state.remaining_attempts, 0)
        self.assertEqual(entry.blocked_reason, "")

    def test_finished_submitted_or_reset_records_are_not_resume_entries(self):
        for change in ({"isFinished": 1}, {"submitTime": 1900}, {"isReset": 1}, {"status": 5}):
            with self.subTest(change=change):
                data = payload()
                data["examRecord"].update(id="record", status=2, startTime=1500)
                data["examRecord"].update(change)
                self.assertFalse(parse_exam_entry(data, EXAM_ID).can_resume)

    def test_entry_requirements_and_attempt_limits_are_preserved(self):
        for change, reason in (
            ({"isSetAccessSecret": 1}, "entry_verification_required"),
            ({"isSetPersonalCode": 1}, "entry_verification_required"),
            ({"clientType": 2}, "entry_unavailable"),
            ({"status": 1}, "entry_unavailable"),
            ({"status": 7}, "entry_unavailable"),
            ({"startTime": 2100}, "entry_unavailable"),
            ({"endTime": 1900}, "entry_unavailable"),
            ({"needApplicant": 1, "signUp": {"status": 1}}, "entry_unavailable"),
            ({"signUp": {"status": 3}}, "entry_unavailable"),
            ({"paperClass": None}, "state_unknown"),
            ({"examedTimes": 3}, "attempt_limit"),
        ):
            with self.subTest(change=change):
                data = payload()
                data.update(change)
                self.assertEqual(parse_exam_entry(data, EXAM_ID).blocked_reason, reason)

    def test_mismatched_or_incomplete_api_responses_are_rejected(self):
        for data in ({}, {"id": "wrong", "examRecord": {}}, {"id": EXAM_ID}, []):
            with self.subTest(data=data):
                with self.assertRaises(ExamEntryUnavailable):
                    parse_exam_entry(data, EXAM_ID)


class ExamEntryRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_record_is_fetched_from_the_official_api(self):
        page = object()
        with (
            patch("core.exam.entry_api.get_authorization_header", new=AsyncMock(return_value="test-token")),
            patch("core.exam.entry_api.fetch_json", new=AsyncMock(return_value={"data": payload()})) as fetch,
        ):
            entry = await read_exam_entry(page, DETAIL_URL + "?from=manual")
        self.assertEqual(entry.answer_url, ANSWER_URL)
        fetch.assert_awaited_once_with(
            page, f"https://kc.zhixueyun.com/api/v1/exam/exam/front/user-record/{EXAM_ID}",
            headers={"Authorization": "test-token", "Version": "12.1.1", "Accept": "application/json"},
        )

    async def test_missing_auth_and_unrecognized_urls_never_request_an_api(self):
        with (
            patch("core.exam.entry_api.get_authorization_header", new=AsyncMock(return_value="")),
            patch("core.exam.entry_api.fetch_json", new=AsyncMock()) as fetch,
        ):
            for url in (DETAIL_URL, "https://example.com/exam"):
                with self.subTest(url=url), self.assertRaises(ExamEntryUnavailable):
                    await read_exam_entry(object(), url)
        fetch.assert_not_awaited()

    async def test_validation_uses_platform_post_and_requires_explicit_success(self):
        page = object()
        entry = parse_exam_entry(payload(), EXAM_ID)
        for status, reason in ((1, None), (0, "entry_unavailable"), (2, "entry_unavailable"), (3, "attempt_limit"), (None, "state_unknown"), (99, "state_unknown"), (True, "state_unknown"), ([], "state_unknown")):
            with (
                self.subTest(status=status),
                patch("core.exam.entry_api.get_authorization_header", new=AsyncMock(return_value="test-token")),
                patch("core.exam.entry_api.fetch_json", new=AsyncMock(return_value={"status": status})) as fetch,
            ):
                if reason is None:
                    await validate_exam_entry(page, entry)
                else:
                    with self.assertRaises(ExamEntryUnavailable) as raised:
                        await validate_exam_entry(page, entry)
                    self.assertEqual(raised.exception.reason, reason)
                self.assertEqual(fetch.call_args.kwargs["method"], "POST")
                self.assertEqual(fetch.call_args.kwargs["data"], f"examId={EXAM_ID}")
                self.assertIn("application/x-www-form-urlencoded", fetch.call_args.kwargs["headers"]["Content-Type"])
                self.assertTrue(fetch.call_args.args[1].endswith("/valiate-exam"))

    async def test_password_required_entry_is_not_validated_or_bypassed(self):
        data = payload()
        data["isSetAccessSecret"] = 1
        with patch("core.exam.entry_api.fetch_json", new=AsyncMock()) as fetch:
            with self.assertRaises(ExamEntryUnavailable):
                await validate_exam_entry(object(), parse_exam_entry(data, EXAM_ID))
        fetch.assert_not_awaited()
