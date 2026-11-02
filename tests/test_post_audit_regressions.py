"""Behavior regressions R1-R9 from the post-implementation audit."""

from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.abort import BrowserCertificateError
from core.discovery.contracts import CollectionResult
from core.tasks.identity import parse_entry
from core.tasks.repository import TaskRepository, now
from core.tasks.relations import record_discovered_tasks
from core.tasks.store import repository_for
from core.queues.learning import (
    record_learning_failure,
    read_learning_failures,
    remove_learning_failure,
    append_learning_urls,
)
from core.queues.exam import (
    append_exam_urls,
    has_ai_failed_model_config,
    read_exam_queue,
)
from core.queues.history import record_submission_intent, pending_submission
from core.workflow.contracts import TaskOutcome, Status
from core.workflow.host import VERSION, activity_context

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
COURSE = "https://kc.zhixueyun.com/#/study/course/detail/" + A
OTHER = COURSE.replace(A, B)
SUBJECT = COURSE.replace("/course/", "/subject/")
EXAM = "https://kc.zhixueyun.com/#/exam/exam/answer-paper/" + A
OTHER_EXAM = EXAM.replace(A, B)
ACCOUNT = "audit-account"


def box(section_id, kind):
    wrapper = SimpleNamespace(
        inner_text=AsyncMock(return_value="必修 synthetic 已完成")
    )
    return SimpleNamespace(
        get_attribute=AsyncMock(
            side_effect=lambda attr: (
                kind if attr == "data-sectiontype" else "D201show-sections" + section_id
            )
        ),
        locator=lambda _: wrapper,
    )


class StateRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.failures = self.root / "挂课失败链接.json"
        self.exams = self.root / "考试链接.json"
        self.repo = repository_for(self.failures)
        for target in ("core.tasks.store.account_id", "core.tasks.identity.account_id"):
            p = patch(target, return_value=ACCOUNT)
            p.start()
            self.addCleanup(p.stop)

    def task(self, url):
        return self.repo.upsert(parse_entry(url, ACCOUNT))

    def finish(self, task, status=Status.COMPLETED, kind="learn", **kwargs):
        run = self.repo.plan(task, kind, {}, VERSION)
        return self.repo.save_outcome(
            run["run_id"], TaskOutcome(task, status, **kwargs).to_dict(), finalized=True
        )

    def test_R1_snapshot_rewrite_never_attaches_foreign_tasks_or_copies_intents(self):
        parent = self.task(COURSE)
        record_learning_failure(
            OTHER,
            reason="sync_timeout",
            reason_text="unrelated",
            file_path=self.failures,
        )
        append_exam_urls([OTHER_EXAM], file_path=self.exams)
        self.repo.intent(parent, "submit", {"record_id": "belongs-only-to-course-A"})
        token = activity_context.set({"task_id": parent, "account": ACCOUNT})
        try:
            remove_learning_failure(
                COURSE, reason="sync_timeout", file_path=self.failures
            )
            append_exam_urls([EXAM], file_path=self.exams)
        finally:
            activity_context.reset(token)
        self.assertEqual(self.repo.children(parent), [])
        self.assertEqual(
            self.repo.pending_intents(
                parse_entry(OTHER_EXAM, ACCOUNT).key.task_id, "submit"
            ),
            [],
        )
        self.assertEqual(self.finish(parent)["status"], "COMPLETED")
        record_discovered_tasks(
            [EXAM],
            parent_url=COURSE,
            source="course_exam_api",
            course_exam=True,
            repository=self.repo,
            account=ACCOUNT,
        )
        self.assertEqual(
            [c["task_id"] for c in self.repo.children(parent)],
            [parse_entry(EXAM, ACCOUNT).key.task_id],
        )
        self.assertEqual(
            len(
                self.repo.pending_intents(
                    parse_entry(EXAM, ACCOUNT).key.task_id, "submit"
                )
            ),
            1,
        )
        self.assertEqual(
            self.repo.pending_intents(
                parse_entry(OTHER_EXAM, ACCOUNT).key.task_id, "submit"
            ),
            [],
        )

    def test_R2_alias_import_unions_failures_and_sticky_manual_protection(self):
        from core.tasks.migration import migrate
        from core.queues.exam import requires_manual_submission, write_exam_urls

        rows = [
            {
                "url": EXAM.replace("answer-paper", "exam-detail"),
                "ai_failed_model_configs": [{"model": "A", "account": ACCOUNT}],
                "requires_manual_submit": True,
            },
            {
                "url": EXAM,
                "ai_failed_model_configs": [{"model": "B", "account": ACCOUNT}],
            },
        ]
        self.exams.write_text(json.dumps(rows), encoding="utf-8")
        migrate(self.root, self.repo, account=ACCOUNT)
        migrate(self.root, self.repo, account=ACCOUNT)
        self.assertEqual(len(read_exam_queue(self.exams)), 1)
        for model in ("A", "B"):
            self.assertTrue(
                has_ai_failed_model_config(
                    EXAM, {"model": model, "account": ACCOUNT}, file_path=self.exams
                )
            )
        write_exam_urls([], file_path=self.exams)
        self.assertTrue(requires_manual_submission(EXAM, file_path=self.exams))
        self.assertTrue(
            has_ai_failed_model_config(
                EXAM, {"model": "A", "account": ACCOUNT}, file_path=self.exams
            )
        )

    def test_R8_child_completion_converges_all_ancestors_but_preserves_local_block(
        self,
    ):
        grand = self.task(SUBJECT)
        parent = self.task(SUBJECT.replace(A, B))
        other_parent = self.task(
            "https://kc.zhixueyun.com/#/train-new/class-detail/" + A
        )
        child = self.task(COURSE)
        optional = self.task(OTHER)
        blocked = self.task("https://kc.zhixueyun.com/#/topic/incomplete")
        self.repo.link(grand, parent)
        for p in (parent, other_parent, blocked):
            self.repo.link(p, child)
        self.repo.link(parent, optional, required=False)
        for p in (grand, parent, other_parent):
            self.finish(
                p,
                Status.WAITING_CHILDREN,
                kind="discover",
                details={"discovery_complete": True},
            )
        self.finish(
            blocked,
            Status.NEEDS_REVIEW,
            kind="discover",
            reason_codes=["discovery_incomplete"],
            details={"discovery_complete": False},
        )
        self.finish(child)
        for p in (grand, parent, other_parent):
            self.assertEqual(self.repo.task(p)["status"], "COMPLETED")
        self.assertEqual(self.repo.task(blocked)["status"], "NEEDS_REVIEW")
        self.assertEqual(self.repo.task(optional)["status"], "READY")
        with self.assertRaises(ValueError):
            self.repo.link(child, grand)

    def test_R8_resolving_last_manual_issue_completes_verified_parent(self):
        parent = self.task(COURSE)
        record_learning_failure(
            COURSE,
            reason="h5_manual_required",
            reason_text="manual issue",
            file_path=self.failures,
        )
        self.finish(
            parent,
            Status.WAITING_CHILDREN,
            reason_codes=["h5_manual_required"],
            details={"local_work_complete": True},
        )
        remove_learning_failure(
            COURSE, reason="h5_manual_required", file_path=self.failures
        )
        self.assertEqual(self.repo.task(parent)["status"], "COMPLETED")
        self.assertEqual(
            json.loads(self.repo.task(parent)["outcome"])["reason_codes"], []
        )

    def test_R1_R2_existing_state_repair_is_backed_up_additive_and_idempotent(self):
        from core.tasks.repair import repair_state

        parent = self.task(COURSE)
        foreign = self.task(OTHER)
        exam = self.task(OTHER_EXAM)
        self.task(EXAM)
        original = self.repo.intent(parent, "submit", {"record_id": "source"})
        copied = hashlib.sha256((original["action_id"] + exam).encode()).hexdigest()
        rows = [
            {
                "url": EXAM.replace("answer-paper", "exam-detail"),
                "ai_failed_model_configs": [{"model": "lost", "account": ACCOUNT}],
            },
            {
                "url": EXAM,
                "ai_failed_model_configs": [{"model": "survived", "account": ACCOUNT}],
            },
        ]
        backup = self.repo.path.parent / "backups/original"
        backup.mkdir(parents=True)
        raw = json.dumps(rows).encode()
        (backup / "考试链接.json").write_bytes(raw)
        with self.repo.connect() as db:
            for child in (foreign, exam):
                db.execute("INSERT INTO task_edges VALUES(?,?,1)", (parent, child))
            db.execute(
                "INSERT INTO action_intents VALUES(?,?,?,'unverified',?,NULL,?)",
                (copied, exam, "submit", original["baseline"], now()),
            )
            db.execute(
                "INSERT INTO imports VALUES(?,?,?)",
                (str(self.exams.resolve()), hashlib.sha256(raw).hexdigest(), now()),
            )
        report = repair_state(self.repo, self.root, apply=True)
        self.assertEqual(report["quarantined_edges"], 2)
        self.assertEqual(report["quarantined_intents"], 1)
        self.assertTrue(Path(report["backup"]).exists())
        self.assertEqual(self.repo.children(parent), [])
        self.assertEqual(len(self.repo.pending_intents(parent, "submit")), 1)
        self.assertEqual(self.repo.task(parent)["status"], "NEEDS_REVIEW")
        self.assertEqual(self.repo.pending_intents(exam, "submit"), [])
        self.assertTrue(
            has_ai_failed_model_config(
                EXAM, {"model": "lost", "account": ACCOUNT}, file_path=self.exams
            )
        )
        from core.queues.exam import requires_manual_submission

        self.assertTrue(requires_manual_submission(OTHER_EXAM, file_path=self.exams))
        self.assertEqual(
            repair_state(self.repo, self.root, apply=True), {"already_repaired": True}
        )

    def test_repair_failure_rolls_back_and_keeps_backup_for_retry(self):
        from core.tasks import repair

        parent = self.task(COURSE)
        foreign = self.task(OTHER)
        with self.repo.connect() as db:
            db.execute("INSERT INTO task_edges VALUES(?,?,1)", (parent, foreign))
        with patch.object(
            self.repo, "refresh_ancestors", side_effect=OSError("simulated disk full")
        ):
            with self.assertRaises(OSError):
                repair.repair_state(self.repo, self.root, apply=True)
        self.assertEqual(len(self.repo.children(parent)), 1)
        with self.repo.connect() as db:
            self.assertFalse(
                db.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone()
            )
        self.assertTrue(
            list((self.repo.path.parent / "backups").glob("repair-v2-*/state.sqlite"))
        )
        self.assertEqual(
            repair.repair_state(self.repo, self.root, apply=True)["quarantined_edges"],
            1,
        )

    def test_missing_original_can_be_restored_later_without_requeue(self):
        from core.tasks.repair import repair_state

        self.task(EXAM)
        raw = json.dumps(
            [
                {
                    "url": EXAM,
                    "ai_failed_model_configs": [
                        {"model": "missing", "account": ACCOUNT}
                    ],
                }
            ]
        ).encode()
        with self.repo.connect() as db:
            db.execute(
                "INSERT INTO imports VALUES(?,?,?)",
                (str(self.exams.resolve()), hashlib.sha256(raw).hexdigest(), now()),
            )
        self.assertEqual(
            repair_state(self.repo, self.root, apply=True)["missing_source_snapshots"],
            1,
        )
        backup = self.repo.path.parent / "backups/recovered"
        backup.mkdir(parents=True)
        (backup / self.exams.name).write_bytes(raw)
        report = repair_state(self.repo, self.root, apply=True, recheck=True)
        self.assertEqual(report["missing_source_snapshots"], 0)
        self.assertTrue(
            has_ai_failed_model_config(
                EXAM, {"model": "missing", "account": ACCOUNT}, file_path=self.exams
            )
        )
        self.assertEqual(read_exam_queue(self.exams), [])


class ActivityRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_R8_subject_inline_course_completion_settles_its_existing_child(self):
        from core.learning import flows
        from tests.workflow_harness import ActivityHost
        from unittest.mock import MagicMock

        parent = self.repo.upsert(parse_entry(SUBJECT, ACCOUNT))
        child = self.repo.upsert(parse_entry(COURSE, ACCOUNT))
        self.repo.link(parent, child)
        host = ActivityHost(self.root, {}, account=ACCOUNT)
        page = MagicMock()
        page.url = SUBJECT
        page.wait_for_load_state = AsyncMock()
        items = MagicMock()
        items.count = AsyncMock(return_value=1)
        items.last.wait_for = AsyncMock()
        items.locator.return_value.last.wait_for = AsyncMock()
        items.nth.return_value.locator.return_value.inner_text = AsyncMock(
            return_value="课程"
        )
        page.locator.return_value = items
        detail = SimpleNamespace(url=COURSE, close=AsyncMock())
        token = activity_context.set({"task_id": parent, "account": ACCOUNT})
        try:
            with (
                patch("core.workflow.host._host", host),
                patch.object(flows, "ensure_course_page_ready", new=AsyncMock()),
                patch.object(
                    flows,
                    "_open_subject_item_popup",
                    new=AsyncMock(return_value=detail),
                ),
                patch.object(
                    flows, "course_learning", new=AsyncMock(return_value=True)
                ),
            ):
                self.assertTrue(await flows.subject_learning(page))
            run = self.repo.plan(parent, "learn", {}, VERSION)
            self.assertEqual(
                self.repo.save_outcome(
                    run["run_id"],
                    TaskOutcome(parent, Status.COMPLETED).to_dict(),
                    finalized=True,
                )["status"],
                "COMPLETED",
            )
            self.assertEqual(self.repo.task(child)["status"], "COMPLETED")
        finally:
            activity_context.reset(token)

    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = TaskRepository(self.root / "state.sqlite")
        self.enterContext(patch("core.tasks.store.account_id", return_value=ACCOUNT))
        self.enterContext(patch("core.tasks.identity.account_id", return_value=ACCOUNT))
        self.enterContext(
            patch("core.tasks.store.repository_for", return_value=self.repo)
        )

    async def test_R3_partial_discovery_survives_child_completion_and_complete_retry_clears_only_discovery(
        self,
    ):
        from core.workflow.activities import discover
        from tests.workflow_harness import ActivityHost

        host = ActivityHost(self.root, {"discover": discover}, account=ACCOUNT)
        self.enterContext(patch("core.workflow.host._host", host))
        zone = "https://kc.zhixueyun.com/#/topic/test"

        @asynccontextmanager
        async def browser():
            yield None, object()

        async def partial(*args, **kwargs):
            record_learning_failure(
                zone, reason="discovery_incomplete", reason_text="pagination incomplete"
            )
            append_learning_urls([COURSE])
            record_discovered_tasks(
                [COURSE],
                parent_url=zone,
                source="entry_links",
                repository=self.repo,
                account=ACCOUNT,
            )
            return CollectionResult(1, False, ("discovery_incomplete",))

        with (
            patch("core.browser.session.create_browser_context", browser),
            patch(
                "core.learning.zone.collect_learning_links_from_learning_zone_urls",
                new=AsyncMock(side_effect=partial),
            ) as collector,
        ):
            outcome = await host.execute(
                zone, "discover", options={"zone_mode": "auto"}
            )
            self.assertEqual(outcome["status"], "NEEDS_REVIEW")
            self.assertIn(
                "discovery_incomplete",
                [e.reason for e in read_learning_failures() if e.url == zone],
            )
            child = parse_entry(COURSE, ACCOUNT).key.task_id
            run = self.repo.plan(child, "learn", {}, VERSION)
            self.repo.save_outcome(
                run["run_id"],
                TaskOutcome(child, Status.COMPLETED).to_dict(),
                finalized=True,
            )
            self.assertEqual(
                self.repo.task(outcome["task_id"])["status"], "NEEDS_REVIEW"
            )
            collector.side_effect = None
            collector.return_value = CollectionResult(0, True)
            outcome = await host.execute(
                zone, "discover", options={"zone_mode": "auto"}
            )
            self.assertEqual(outcome["status"], "COMPLETED")
            self.assertFalse([e for e in read_learning_failures() if e.url == zone])

    async def test_R4_clear_persists_review_dismissal_and_explicit_readd_can_resume(
        self,
    ):
        from core.queues.manage import clear_link_queues
        from core.app.service import ApplicationService
        from tests.workflow_harness import ActivityHost

        failures = self.root / "挂课失败链接.json"
        learning = self.root / "课程链接.json"
        record_learning_failure(
            SUBJECT, reason="url_type_pending", reason_text="review", file_path=failures
        )
        exams = self.root / "考试链接.json"
        record_submission_intent(EXAM, {"model": "test", "account": ACCOUNT}, exams)
        with (
            patch(
                "core.queues.manage.link_queue_paths",
                return_value={"课程待办": learning, "失败 / 复查": failures},
            ),
            patch("core.config.DATA_DIR", self.root),
        ):
            clear_link_queues()
            # A repeated clear must not revive a derived task absent from JSON.
            clear_link_queues()
        service = ApplicationService(ActivityHost(self.root, {}, account=ACCOUNT))
        service.batch = AsyncMock(return_value=[])
        with patch("core.queues.learning.read_learning_urls", return_value=[]):
            await service.learn()
        self.assertEqual(service.batch.await_args.args[0], [])
        self.assertIsNotNone(pending_submission(EXAM, exams, account=ACCOUNT))
        record_learning_failure(
            SUBJECT,
            reason="url_type_pending",
            reason_text="explicitly rediscovered",
            file_path=failures,
        )
        with patch("core.queues.learning.read_learning_urls", return_value=[]):
            await service.learn()
        self.assertEqual(service.batch.await_args.args[0], [SUBJECT])

    async def test_R4_clear_also_dismisses_legacy_latent_reviews_without_queue_rows(
        self,
    ):
        from core.queues.manage import clear_link_queues

        failures = self.root / "挂课失败链接.json"
        learning = self.root / "课程链接.json"
        record_learning_failure(
            SUBJECT, reason="url_type_pending", reason_text="review", file_path=failures
        )
        with self.repo.connect() as db:
            db.execute("DELETE FROM queue_items")
        failures.write_text("[]", encoding="utf-8")
        with (
            patch(
                "core.queues.manage.link_queue_paths",
                return_value={"课程待办": learning, "失败 / 复查": failures},
            ),
            patch("core.config.DATA_DIR", self.root),
        ):
            result = clear_link_queues()
        self.assertIsNotNone(result.backup_dir)
        self.assertEqual(self.repo.learning_reviews(ACCOUNT), [])

    async def test_R4_cleared_issue_does_not_block_fresh_verified_run(self):
        from core.queues.manage import clear_link_queues

        failures = self.root / "挂课失败链接.json"
        learning = self.root / "课程链接.json"
        record_learning_failure(
            COURSE,
            reason="h5_manual_required",
            reason_text="old issue",
            file_path=failures,
        )
        with (
            patch(
                "core.queues.manage.link_queue_paths",
                return_value={"课程待办": learning, "失败 / 复查": failures},
            ),
            patch("core.config.DATA_DIR", self.root),
        ):
            clear_link_queues()
        append_learning_urls([COURSE], file_path=learning)
        parent = parse_entry(COURSE, ACCOUNT).key.task_id
        run = self.repo.plan(parent, "learn", {}, VERSION)
        self.assertEqual(
            self.repo.save_outcome(
                run["run_id"],
                TaskOutcome(parent, Status.COMPLETED).to_dict(),
                finalized=True,
            )["status"],
            "COMPLETED",
        )
        record_learning_failure(
            COURSE,
            reason="h5_manual_required",
            reason_text="newly discovered",
            file_path=failures,
        )
        self.assertEqual(self.repo.task(parent)["status"], "WAITING_CHILDREN")

    async def test_R6_subject_child_certificate_keeps_batch_stop_and_closes_page(self):
        from core.learning import flows
        from unittest.mock import MagicMock

        page = MagicMock()
        page.url = SUBJECT
        page.wait_for_load_state = AsyncMock()
        items = MagicMock()
        items.count = AsyncMock(return_value=2)
        items.last.wait_for = AsyncMock()
        items.locator.return_value.last.wait_for = AsyncMock()
        items.nth.return_value.locator.return_value.inner_text = AsyncMock(
            return_value="课程"
        )
        page.locator.return_value = items
        child = SimpleNamespace(close=AsyncMock())
        with (
            patch.object(flows, "ensure_course_page_ready", new=AsyncMock()),
            patch.object(
                flows, "_open_subject_item_popup", new=AsyncMock(return_value=child)
            ),
            patch.object(
                flows,
                "course_learning",
                new=AsyncMock(
                    side_effect=RuntimeError("Page.goto: net::ERR_CERT_DATE_INVALID")
                ),
            ) as handler,
        ):
            with self.assertRaises(BrowserCertificateError):
                await flows.subject_learning(page)
        handler.assert_awaited_once()
        child.close.assert_awaited_once()

    async def test_R5_verified_success_resolves_startup_failures_preserving_manual_issues(
        self,
    ):
        from core.learning import afk_runner

        path = self.root / "挂课失败链接.json"
        record_learning_failure(
            COURSE,
            reason="course_bootstrap_timeout",
            reason_text="old timeout",
            file_path=path,
        )
        record_learning_failure(
            COURSE,
            reason="course_info_api_error",
            reason_text="old request",
            file_path=path,
        )
        page = SimpleNamespace(
            goto=AsyncMock(), close=AsyncMock(), is_closed=lambda: False
        )
        with (
            patch.object(
                afk_runner, "_open_course_page", new=AsyncMock(return_value=page)
            ),
            patch.object(afk_runner, "_install_course_info_monitor", return_value=None),
            patch.object(afk_runner, "_wait_for_course_bootstrap", new=AsyncMock()),
            patch.object(afk_runner, "ensure_course_page_ready", new=AsyncMock()),
        ):
            result = await afk_runner._process_url(
                object(), COURSE, AsyncMock(return_value=True), failure_file=path
            )
            self.assertEqual(result.status, "COMPLETED")
            self.assertEqual(read_learning_failures(path), [])
            record_learning_failure(
                COURSE,
                reason="h5_manual_required",
                reason_text="independent",
                file_path=path,
            )
            result = await afk_runner._process_url(
                object(), COURSE, AsyncMock(return_value=True), failure_file=path
            )
            self.assertEqual(result.status, "WAITING_CHILDREN")
            self.assertEqual(result.reason_codes, ["h5_manual_required"])

    async def test_R6_chapter_certificate_errors_stop_before_next_chapter(self):
        from core.learning import flows
        from core.workflow.errors import error_outcome

        for error in (
            BrowserCertificateError("ERR_CERT_DATE_INVALID"),
            RuntimeError("Page.click: net::ERR_CERT_DATE_INVALID"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(flows, "prepare_course_page", new=AsyncMock()),
                patch.object(
                    flows,
                    "_collect_chapter_boxes",
                    new=AsyncMock(
                        return_value=[
                            ("required", box(A, "3")),
                            ("required", box(B, "3")),
                        ]
                    ),
                ),
                patch.object(
                    flows,
                    "read_server_section_states",
                    new=AsyncMock(
                        return_value={A: {"finish_status": 1}, B: {"finish_status": 1}}
                    ),
                ),
                patch.object(
                    flows,
                    "queue_course_exams_from_api",
                    new=AsyncMock(return_value=None),
                ),
                patch.object(flows, "_activate_course_section", new=AsyncMock()),
                patch.object(
                    flows, "handle_document", new=AsyncMock(side_effect=[error, None])
                ) as handler,
            ):
                with self.assertRaises(BrowserCertificateError) as caught:
                    await flows.course_learning(SimpleNamespace(url=COURSE))
                self.assertEqual(handler.await_count, 1)
                self.assertEqual(
                    error_outcome("task", caught.exception).stop_scope, "batch"
                )

    async def test_R7_mixed_media_and_passed_exam_accept_their_own_evidence(self):
        from core.learning import flows
        from core.learning.exam_api import CourseExamQueueResult

        with (
            patch.object(flows, "prepare_course_page", new=AsyncMock()),
            patch.object(
                flows,
                "_collect_chapter_boxes",
                new=AsyncMock(
                    return_value=[("required", box(A, "3")), ("required", box(B, "9"))]
                ),
            ),
            patch.object(
                flows,
                "read_server_section_states",
                new=AsyncMock(
                    return_value={A: {"finish_status": 2}, B: {"finish_status": None}}
                ),
            ),
            patch.object(
                flows,
                "queue_course_exams_from_api",
                new=AsyncMock(return_value=CourseExamQueueResult(1, 0, 0, 1)),
            ) as exams,
            patch.object(
                flows, "_activate_course_section", new=AsyncMock()
            ) as activate,
        ):
            self.assertTrue(await flows.course_learning(SimpleNamespace(url=COURSE)))
            activate.assert_not_awaited()
            exams.return_value = CourseExamQueueResult(1, 0, 1, 0)
            self.assertFalse(await flows.course_learning(SimpleNamespace(url=COURSE)))

    async def test_R7_new_exam_evidence_resolves_legacy_media_section_dependency(self):
        from core.learning import flows
        from core.learning.exam_api import CourseExamQueueResult
        from core.learning.course_session import CompletionPolicy
        from tests.workflow_harness import ActivityHost

        host = ActivityHost(self.root, {}, account=ACCOUNT)
        parent = self.repo.upsert(parse_entry(COURSE, ACCOUNT))
        token = activity_context.set({"task_id": parent, "account": ACCOUNT})
        try:
            with patch("core.workflow.host._host", host):
                CompletionPolicy.record({B: {"finish_status": None}})
                with (
                    patch.object(flows, "prepare_course_page", new=AsyncMock()),
                    patch.object(
                        flows,
                        "_collect_chapter_boxes",
                        new=AsyncMock(
                            return_value=[
                                ("required", box(A, "3")),
                                ("required", box(B, "9")),
                            ]
                        ),
                    ),
                    patch.object(
                        flows,
                        "read_server_section_states",
                        new=AsyncMock(return_value={A: {"finish_status": 2}}),
                    ),
                    patch.object(
                        flows,
                        "queue_course_exams_from_api",
                        new=AsyncMock(return_value=CourseExamQueueResult(1, 0, 0, 1)),
                    ),
                ):
                    self.assertTrue(
                        await flows.course_learning(SimpleNamespace(url=COURSE))
                    )
                run = self.repo.plan(parent, "learn", {}, VERSION)
                result = self.repo.save_outcome(
                    run["run_id"],
                    TaskOutcome(parent, Status.COMPLETED).to_dict(),
                    finalized=True,
                )
                self.assertEqual(result["status"], "COMPLETED")
                self.assertTrue(
                    all(c["status"] == "COMPLETED" for c in self.repo.children(parent))
                )
        finally:
            activity_context.reset(token)
