from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.tasks.identity import parse_entry
from core.tasks.store import repository_for
from core.queues.exam import (
    append_exam_urls,
    write_exam_urls,
    requires_manual_submission,
)
from core.queues.history import record_submission_intent, pending_submission
from core.queues.learning import (
    record_learning_failure,
    read_learning_failures,
    remove_learning_failure,
)
from core.learning.course_session import PhaseBudget
from core.workflow.host import activity_context
from tests.workflow_harness import ActivityHost

UUID = "11111111-1111-4111-8111-111111111111"
SOURCE = "22222222-2222-4222-8222-222222222222"
COURSE = "https://kc.zhixueyun.com/#/study/course/detail/" + UUID
EXAM = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/" + UUID


class IdentityAndIssuesTests(unittest.TestCase):
    def test_A1_alias_intents_and_sticky_manual_protection_survive_queue_clear(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "考试链接.json"
            append_exam_urls([EXAM], file_path=path, requires_manual_submit=True)
            record_submission_intent(EXAM, {"account": "a", "model": "test"}, path)
            write_exam_urls([], file_path=path)
            alias = EXAM.replace("answer-paper", "exam-detail") + "?from=other"
            self.assertTrue(requires_manual_submission(alias, file_path=path))
            self.assertIsNotNone(pending_submission(alias, path, account="a"))
            self.assertIsNone(pending_submission(alias, path, account="b"))

    def test_A3_text_and_href_keep_exact_course_source(self):
        from core.learning.zone import (
            extract_learning_links_from_learning_zone_html,
            extract_learning_links_from_runtime_values,
        )

        url = COURSE + "/6/1/" + SOURCE
        self.assertEqual(
            extract_learning_links_from_learning_zone_html(f'<a href="{url}">课程</a>'),
            [url],
        )
        self.assertEqual(extract_learning_links_from_runtime_values([url]), [url])

    def test_A7_parent_keeps_independent_issues_and_clears_only_one(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "挂课失败链接.json"
            for reason in ("url_type_pending", "other_learning_type"):
                record_learning_failure(
                    COURSE, reason=reason, reason_text=reason, file_path=path
                )
            self.assertEqual(len(read_learning_failures(path)), 2)
            repo = repository_for(path)
            parent = parse_entry(COURSE, "unbound").key.task_id
            self.assertEqual(len(repo.children(parent)), 2)
            remove_learning_failure(COURSE, reason="url_type_pending", file_path=path)
            self.assertEqual(
                [e.reason for e in read_learning_failures(path)],
                ["other_learning_type"],
            )
            self.assertEqual(
                sum(c["status"] == "NEEDS_REVIEW" for c in repo.children(parent)), 1
            )


class CourseAndEnrollmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_A2_all_dom_fast_paths_require_fresh_server_evidence(self):
        from core.learning import flows
        from core.abort import SyncTimeoutError

        wrapper = SimpleNamespace(
            inner_text=AsyncMock(return_value="必修 文档 05:00 已完成")
        )
        box = SimpleNamespace(
            get_attribute=AsyncMock(
                side_effect=lambda name: (
                    "3" if name == "data-sectiontype" else "D201show-sections" + UUID
                )
            ),
            locator=lambda _: wrapper,
        )
        with (
            patch.object(flows, "prepare_course_page", new=AsyncMock()),
            patch.object(
                flows,
                "_collect_chapter_boxes",
                new=AsyncMock(return_value=[("required", box)]),
            ),
            patch.object(
                flows,
                "read_server_section_states",
                new=AsyncMock(return_value={UUID: {"finish_status": 1}}),
            ) as read,
            patch.object(
                flows, "queue_course_exams_from_api", new=AsyncMock(return_value=None)
            ),
            patch.object(flows, "_activate_course_section", new=AsyncMock()),
            patch.object(flows, "handle_document", new=AsyncMock()),
        ):
            with self.assertRaises(SyncTimeoutError):
                await flows.course_learning(SimpleNamespace(url=COURSE))
            self.assertGreaterEqual(read.await_count, 2)

    async def test_enrollment_lost_response_reconciles_and_unknown_never_resends(self):
        from core.discovery import train_class

        with TemporaryDirectory() as tmp:
            host = ActivityHost(tmp, {})
            url = "https://kc.zhixueyun.com/#/train-new/class-detail/" + UUID
            task_id = host.repository.upsert(parse_entry(url, "test-account"))
            payload = {
                "run_id": "synthetic",
                "task_id": task_id,
                "account": "test-account",
            }
            host.scopes["synthetic"] = payload
            token = activity_context.set(payload)
            try:
                progress = {"registerStatus": 0, "classInfoVo": {"payType": 0}}
                with (
                    patch("core.workflow.host._host", host),
                    patch.object(
                        train_class,
                        "_registration_progress",
                        new=AsyncMock(return_value=progress),
                    ) as query,
                    patch.object(
                        train_class,
                        "fetch_json",
                        new=AsyncMock(side_effect=ConnectionError("lost response")),
                    ) as write,
                ):
                    with self.assertRaises(ConnectionError):
                        await train_class.ensure_class_enrolled(
                            object(), UUID, headers={}
                        )
                    with self.assertRaises(train_class.ClassEnrollmentError):
                        await train_class.ensure_class_enrolled(
                            object(), UUID, headers={}
                        )
                    write.assert_awaited_once()
                    query.return_value = {"registerStatus": 5}
                    await train_class.ensure_class_enrolled(object(), UUID, headers={})
                    write.assert_awaited_once()
                    self.assertEqual(
                        host.repository.pending_intents(task_id, "enroll"), []
                    )
            finally:
                activity_context.reset(token)

    async def test_confirmation_budget_survives_restart_and_watch_ignores_offline_time(
        self,
    ):
        from datetime import datetime, timedelta, timezone
        from core.workflow.host import VERSION

        with TemporaryDirectory() as tmp:
            host = ActivityHost(tmp, {})
            task_id = host.repository.upsert(parse_entry(COURSE, "test-account"))
            run = host.repository.plan(task_id, "learn", {}, VERSION)
            token = activity_context.set({"run_id": run["run_id"], "task_id": task_id})
            try:
                with patch("core.workflow.host._host", host):
                    sync = PhaseBudget(60, "sync")
                    watch = PhaseBudget(120, "watch", offline_consumes=False)
                    sync.save()
                    watch.save()
                    with host.repository.connect() as db:
                        db.execute(
                            "UPDATE phases SET started_at=?,consumed=10",
                            (
                                (
                                    datetime.now(timezone.utc) - timedelta(seconds=90)
                                ).isoformat(),
                            ),
                        )
                    self.assertEqual(PhaseBudget(60, "sync").remaining, 0)
                    self.assertGreater(
                        PhaseBudget(120, "watch", offline_consumes=False).remaining, 109
                    )
            finally:
                activity_context.reset(token)


class ReadRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_read_is_bounded_and_post_write_is_never_retried(self):
        from core.browser import page_auth

        failed = SimpleNamespace(ok=False, status=503, dispose=AsyncMock())
        request = SimpleNamespace(
            get=AsyncMock(return_value=failed), post=AsyncMock(return_value=failed)
        )
        page = SimpleNamespace(context=SimpleNamespace(request=request))
        with patch.object(page_auth.asyncio, "sleep", new=AsyncMock()):
            for method, count in (("GET", 3), ("POST", 1)):
                with self.assertRaises(RuntimeError):
                    await page_auth.fetch_json(
                        page,
                        "https://kc.zhixueyun.com/api/synthetic",
                        headers={},
                        method=method,
                    )
                self.assertEqual(
                    (request.get if method == "GET" else request.post).await_count,
                    count,
                )
        self.assertEqual(failed.dispose.await_count, 4)
