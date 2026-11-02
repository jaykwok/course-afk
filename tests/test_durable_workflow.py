from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from core.tasks.identity import parse_entry
from core.tasks.repository import TaskRepository
from core.tasks.migration import dry_run, migrate


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

    def test_process_death_during_migration_rolls_back_then_imports_once(self):
        import sqlite3

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            links = root / "links"
            links.mkdir()
            (links / "课程链接.json").write_text(
                json.dumps(
                    [
                        {
                            "url": "https://kc.zhixueyun.com/#/study/course/detail/11111111-1111-4111-8111-111111111111"
                        }
                    ]
                ),
                encoding="utf-8",
            )
            self.worker("crash-migration", root, 86)
            repo = TaskRepository(root / "workflows/state.sqlite")
            self.assertEqual(repo.export()["tasks"], [])
            self.assertEqual(repo.export()["imports"], [])
            self.worker("recover-migration", root)
            self.assertEqual(len(repo.export()["tasks"]), 1)
            for backup in (root / "workflows/backups").glob("*/state.sqlite"):
                db = sqlite3.connect(backup)
                try:
                    self.assertEqual(
                        db.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                    )
                finally:
                    db.close()


class MigrationTests(unittest.TestCase):
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

    def test_transaction_import_is_idempotent_and_keeps_ambiguous_history(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            links = root / "links"
            links.mkdir()
            url = "https://kc.zhixueyun.com/#/exam/exam-detail/11111111-1111-4111-8111-111111111111"
            (links / "考试链接.json").write_text(
                json.dumps(
                    [{"url": url}, {"url": url.replace("exam-detail", "answer-paper")}]
                ),
                encoding="utf-8",
            )
            history = {
                "version": 2,
                "entries": {
                    u: {
                        "failed_models": [{"model": str(i)}],
                        "submissions": {"a": {"state": "unverified", "at": str(i)}},
                    }
                    for i, u in enumerate(
                        (url, url.replace("exam-detail", "answer-paper"))
                    )
                },
            }
            (links / "exam-history.json").write_text(
                json.dumps(history), encoding="utf-8"
            )
            report = dry_run(links)
            self.assertTrue(report["unbound"])
            repo = TaskRepository(root / "state.sqlite")
            migrate(links, repo)
            migrate(links, repo)
            data = repo.export()
            self.assertEqual(len(data["tasks"]), 1)
            self.assertEqual(len(data["action_intents"]), 2)
            self.assertEqual(len(data["queue_items"]), 1)
            self.assertEqual(
                len(json.loads(data["exam_history"][0]["payload"])["failed_models"]), 2
            )
            (links / "课程链接.json").write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                migrate(links, repo)
            self.assertEqual(repo.export()["tasks"], data["tasks"])
