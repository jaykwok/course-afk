"""Fresh deployments use SQLite and never import existing JSON files."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from core.auth.credential import load_credential_metadata, load_pending_owner_metadata
from core.file_ops import load_cookies
from core.queues.exam import read_exam_queue
from core.queues.learning import read_learning_failures, read_learning_queue
from core.queues.manual_exam import read_manual_exam_queue
from core.tasks.store import read_history, read_queue, repository_for, write_queue


class FreshStoreTests(unittest.TestCase):
    def test_existing_queue_and_history_files_do_not_populate_new_store(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, reader in (
                ("课程链接.json", read_learning_queue),
                ("挂课失败链接.json", read_learning_failures),
                ("考试链接.json", read_exam_queue),
                ("人工考试链接.json", read_manual_exam_queue),
            ):
                path = root / name
                raw = json.dumps(
                    [{"url": "https://example.com/old", "reason": "url_type_pending"}]
                ).encode()
                path.write_bytes(raw)
                self.assertEqual(reader(path), [])
                self.assertEqual(path.read_bytes(), raw)
            history = root / "exam-history.json"
            raw = json.dumps(
                {
                    "version": 2,
                    "entries": {
                        "https://example.com/old": {"requires_manual_submit": True}
                    },
                }
            ).encode()
            history.write_bytes(raw)
            self.assertEqual(read_history(root / "考试链接.json")["entries"], {})
            self.assertEqual(history.read_bytes(), raw)
            data = repository_for(history).export()
            self.assertEqual(data["tasks"], [])
            self.assertEqual(data["action_intents"], [])

    def test_snapshot_edits_and_corruption_cannot_change_committed_queue(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "课程链接.json"
            rows = [{"url": "https://example.com/new"}]
            write_queue(path, rows)
            for raw in (b"[]", b"not-json", b'[{"url":"https://example.com/foreign"}]'):
                path.write_bytes(raw)
                self.assertEqual(read_queue(path), rows)
                self.assertEqual(path.read_bytes(), raw)
            path.unlink()
            self.assertEqual(read_queue(path), rows)

    def test_cookie_arrays_and_separate_metadata_are_not_login_credentials(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "cookies.json"
            path.write_text(
                '[{"name":"authorization","value":"test-only"}]', encoding="utf-8"
            )
            (root / "credential_meta.json").write_text(
                '{"account_name":"old"}', encoding="utf-8"
            )
            with patch("core.auth.credential.COOKIES_FILE", path):
                self.assertIsNone(load_credential_metadata())
                self.assertIsNone(load_pending_owner_metadata())
            with self.assertRaises(ValueError):
                load_cookies(path)
