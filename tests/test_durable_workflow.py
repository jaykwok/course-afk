from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from core.tasks.identity import parse_entry
from core.tasks.repository import TaskRepository


class DurableWorkflowTests(unittest.TestCase):
    def worker(self, mode, path, expected=0):
        result = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(Path(__file__).with_name("workflow_worker.py")),
                mode,
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=40,
        )
        self.assertEqual(result.returncode, expected, result.stderr[-6000:])

    def events(self, path):
        return Counter(
            json.loads(line)
            for line in (path / "events.jsonl").read_text().splitlines()
        )

    def test_effect_before_checkpoint_reconciles_after_idle_start(self):
        with TemporaryDirectory(prefix="工作流 space ") as tmp:
            path = Path(tmp)
            self.worker("crash-effect", path, 86)
            self.worker("idle", path)
            self.assertEqual(self.events(path)["activity_entered"], 1)
            self.worker("recover", path)
            counts = self.events(path)
            self.assertEqual(counts["external_write"], 1)
            self.assertEqual(counts["reconciled"], 1)
            self.assertEqual(counts["activity_entered"], 2)

    def test_dispatch_intent_and_business_commit_windows(self):
        for fault in ("crash-plan", "crash-outcome"):
            with self.subTest(fault=fault), TemporaryDirectory() as tmp:
                path = Path(tmp)
                self.worker(fault, path, 86)
                self.worker("recover", path)
                self.assertEqual(self.events(path)["external_write"], 1)
                self.assertEqual(self.events(path)["activity_entered"], 1)
                repo = TaskRepository(path / "workflows" / "state.sqlite")
                self.assertEqual(len(repo.export()["runs"]), 1)
                self.assertEqual(repo.export()["runs"][0]["dispatch"], "FINISHED")

    def test_cancel_waits_for_activity_finally(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            self.worker("cancel", path)
            self.assertEqual(self.events(path)["cleanup"], 1)
            self.assertEqual(self.events(path)["cancel_drained"], 1)

    def test_resumed_progress_uses_current_frontend_and_does_not_leak(self):
        for restart in (False, True):
            with self.subTest(restart=restart), TemporaryDirectory() as tmp:
                path = Path(tmp)
                if restart:
                    self.worker("progress-cancel", path)
                    self.worker("progress-recover", path)
                else:
                    self.worker("progress-resume", path)
                counts = self.events(path)
                self.assertEqual(counts["first_frontend"], 1)
                self.assertEqual(counts["resumed_frontend"], 1)
                self.assertEqual(counts["progress_cleanup"], 1)
                self.assertEqual(counts["progress_finished"], 1)

    def test_account_switch_rejects_old_activity_scope(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            self.worker("wrong-account", path)
            self.assertEqual(self.events(path)["account_rejected"], 1)
            self.assertEqual(self.events(path)["external_write"], 0)

    def test_incompatible_history_requires_review_without_external_activity(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            self.worker("old-version", path)
            self.assertEqual(self.events(path)["version_preserved"], 1)
            self.assertEqual(self.events(path)["external_write"], 0)


class TaskIdentityTests(unittest.TestCase):
    def test_aliases_merge_but_course_sources_and_accounts_stay_distinct(self):
        detail = "https://kc.zhixueyun.com/#/exam/exam-detail/11111111-1111-4111-8111-111111111111"
        self.assertEqual(
            parse_entry(detail, "a").key,
            parse_entry(detail.replace("exam-detail", "answer-paper"), "a").key,
        )
        self.assertNotEqual(parse_entry(detail, "a").key, parse_entry(detail, "b").key)
        course = detail.replace("/exam/exam-detail/", "/study/course/detail/")
        self.assertNotEqual(
            parse_entry(course, "a").key,
            parse_entry(course + "/6/1/22222222-2222-4222-8222-222222222222", "a").key,
        )
        self.assertIsNone(parse_entry(course, "a").key.source_type)
