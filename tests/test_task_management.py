import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import config
from core.queues.manage import clear_link_queues, link_queue_paths, snapshot_link_queues


class QueueFixture(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {
            "LEARNING_URLS_FILE": self.root / "links/课程链接.json",
            "LEARNING_FAILURES_FILE": self.root / "links/挂课失败链接.json",
            "EXAM_URLS_FILE": self.root / "links/考试链接.json",
            "MANUAL_EXAM_FILE": self.root / "links/人工考试链接.json",
        }
        self.patch = patch.multiple(config, DATA_DIR=self.root, **self.paths)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        for path in self.paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('[{"url":"https://example.com/1"}]', encoding='utf-8')
        self.original = {path: path.read_bytes() for path in self.paths.values()}
        self.history = self.root/'links/exam-history.json'
        self.history.write_text('history', encoding='utf-8')
        self.credentials = self.root/'credentials/cookies.json'
        self.credentials.parent.mkdir()
        self.credentials.write_text('credentials', encoding='utf-8')


class QueueClearTests(QueueFixture):
    def test_default_scope_clears_course_and_all_failure_reasons_with_exact_backups(self):
        result = clear_link_queues()
        self.assertEqual(list(result.counts.values()), [1, 1])
        for name, path in self.paths.items():
            if name.startswith('LEARNING_'):
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')), [])
                self.assertEqual((result.backup_dir/path.name).read_bytes(), self.original[path])
            else:
                self.assertEqual(path.read_bytes(), self.original[path])
        self.assertEqual(self.history.read_text(), 'history')
        self.assertEqual(self.credentials.read_text(), 'credentials')

    def test_all_scope_clears_four_queues_but_keeps_history_and_credentials(self):
        result = clear_link_queues(True)
        for path in self.paths.values():
            self.assertEqual(path.read_bytes(), b'[]\n')
            self.assertEqual((result.backup_dir/path.name).read_bytes(), self.original[path])
        self.assertEqual(self.history.read_text(), 'history')
        self.assertEqual(self.credentials.read_text(), 'credentials')

    def test_cancelled_or_stale_confirmation_does_not_clear(self):
        snapshot = snapshot_link_queues()
        path = self.paths['LEARNING_URLS_FILE']
        path.write_text('[]')
        with self.assertRaisesRegex(RuntimeError, '发生变化'):
            clear_link_queues(expected_snapshot=snapshot)
        self.assertEqual(self.paths['LEARNING_FAILURES_FILE'].read_bytes(), self.original[self.paths['LEARNING_FAILURES_FILE']])
        self.assertFalse((self.root/'backups').exists())

    def test_backup_failure_leaves_all_queues_untouched(self):
        with patch('core.queues.manage.write_bytes_atomic', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                clear_link_queues(True)
        self.assertEqual(snapshot_link_queues(True), self.original)

    def test_partial_clear_failure_restores_earlier_files(self):
        from core.storage import write_bytes_atomic
        failure_path = self.paths['LEARNING_FAILURES_FILE']
        def write(path, content):
            if path == failure_path and content == b'[]\n':
                raise OSError('locked')
            write_bytes_atomic(path, content)
        with patch('core.queues.manage.write_bytes_atomic', side_effect=write):
            with self.assertRaisesRegex(RuntimeError, '已恢复'):
                clear_link_queues(True)
        self.assertEqual(snapshot_link_queues(True), self.original)

    def test_damaged_queue_is_backed_up_before_clearing(self):
        path = self.paths['LEARNING_FAILURES_FILE']
        path.write_bytes(b'not-json')
        result = clear_link_queues()
        self.assertIsNone(result.counts['失败 / 复查'])
        self.assertEqual((result.backup_dir/path.name).read_bytes(), b'not-json')
        self.assertEqual(path.read_bytes(), b'[]\n')

    def test_empty_queues_need_no_backup(self):
        for path in self.paths.values():
            path.write_bytes(b'[]')
        self.assertIsNone(clear_link_queues(True).backup_dir)
        self.assertFalse((self.root/'backups').exists())


class FakeUi:
    def __init__(self, confirmed):
        self.confirmed = confirmed
        self.confirmations = []
        self.summaries = []
        self.messages = []
    def prompt_summary_confirmation(self, title, rows, message, default):
        self.confirmations.append((title, rows, default))
        return self.confirmed
    def show_info(self, message): self.messages.append(message)
    def show_error(self, message): self.messages.append(message)
    def show_summary(self, title, rows): self.summaries.append((title, rows))
    def pause(self): pass


class ClearControllerTests(QueueFixture):
    def test_confirmation_defaults_to_no_and_cancel_keeps_files(self):
        from core.app.launcher_controller import handle_clear_links
        ui = FakeUi(False)
        handle_clear_links(ui)
        self.assertEqual(ui.confirmations[0][2], 'N')
        self.assertEqual(snapshot_link_queues(True), self.original)
        self.assertFalse((self.root/'backups').exists())

    def test_confirmed_clear_shows_backup_and_new_main_state_has_zero_course_counts(self):
        from core.app.launcher_controller import handle_clear_links
        ui = FakeUi(True)
        handle_clear_links(ui)
        self.assertEqual([json.loads(path.read_bytes()) for path in link_queue_paths().values()], [[], []])
        self.assertIn('clear-links-', ui.summaries[0][1][-1][1])

    def test_management_menu_dispatches_only_the_selected_clear_scope(self):
        from core.app.launcher_controller import handle_task_management
        from types import SimpleNamespace
        for choice, expected in ((3, False), (4, True), (5, None)):
            with patch('core.app.launcher_controller.handle_clear_links') as clear:
                ui = SimpleNamespace(prompt_choice=lambda *args: choice)
                handle_task_management(None, None, None, ui)
                if expected is None:
                    clear.assert_not_called()
                elif expected:
                    clear.assert_called_once_with(ui, include_exams=True)
                else:
                    clear.assert_called_once_with(ui)
