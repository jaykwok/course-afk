import logging
import unittest
from unittest.mock import patch
from os import environ
from pathlib import Path
from tempfile import TemporaryDirectory

from core import config


class LoggingConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        data_dir = Path(self.temp_dir.name) / "data"
        credentials_dir = data_dir / "credentials"
        links_dir = data_dir / "links"
        logs_dir = data_dir / "logs"
        references_dir = data_dir / "references"
        self.path_patcher = patch.multiple(
            config,
            DATA_DIR=data_dir,
            CREDENTIALS_DIR=credentials_dir,
            LINKS_DIR=links_dir,
            LOGS_DIR=logs_dir,
            REFERENCE_OUTPUT_DIR=references_dir,
            COOKIES_FILE=credentials_dir / "cookies.json",
            CREDENTIAL_META_FILE=credentials_dir / "credential_meta.json",
            LEARNING_URLS_FILE=links_dir / "课程链接.json",
            LEARNING_FAILURES_FILE=links_dir / "挂课失败链接.json",
            EXAM_URLS_FILE=links_dir / "考试链接.json",
            MANUAL_EXAM_FILE=links_dir / "人工考试链接.json",
            INFO_LOG_FILE=logs_dir / "app-info.log",
            WARN_LOG_FILE=logs_dir / "app-warn.log",
            ERROR_LOG_FILE=logs_dir / "app-error.log",
        )
        self.path_patcher.start()
        self.root_logger = logging.getLogger()
        self.original_handlers = self.root_logger.handlers[:]
        self.original_level = self.root_logger.level
        self.original_flag = getattr(config, "_LOGGING_CONFIGURED", False)
        self.root_logger.handlers.clear()
        config._LOGGING_CONFIGURED = False

    def tearDown(self):
        for handler in self.root_logger.handlers[:]:
            self.root_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        self.root_logger.handlers.extend(self.original_handlers)
        self.root_logger.setLevel(self.original_level)
        config._LOGGING_CONFIGURED = self.original_flag
        self.path_patcher.stop()
        self.temp_dir.cleanup()

    def test_setup_logging_uses_info_level_console_handler(self):
        config.setup_logging()
        handlers = logging.getLogger().handlers
        file_handlers = [
            handler for handler in handlers if isinstance(handler, logging.FileHandler)
        ]
        console_handlers = [
            handler
            for handler in handlers
            if not isinstance(handler, logging.FileHandler)
        ]
        self.assertEqual(len(file_handlers), 3)
        self.assertEqual(len(console_handlers), 1)
        self.assertEqual(console_handlers[0].level, logging.INFO)

    def test_setup_logging_is_idempotent(self):
        config.setup_logging()
        first_count = len(logging.getLogger().handlers)
        config.setup_logging()
        self.assertEqual(len(logging.getLogger().handlers), first_count)

    def test_setup_logging_silences_asyncio_debug_logs(self):
        config.setup_logging()
        self.assertEqual(logging.getLogger("asyncio").level, logging.WARNING)

    def test_setup_logging_can_skip_startup_banner(self):
        with patch("core.config._log_startup_banner") as mock_banner:
            config.setup_logging(show_startup_banner=False)
        mock_banner.assert_not_called()

    def test_setup_logging_can_skip_startup_banner_via_env(self):
        with patch.dict(environ, {"SUPPRESS_STARTUP_BANNER": "1"}, clear=False):
            with patch("core.config._log_startup_banner") as mock_banner:
                config.setup_logging()
        mock_banner.assert_not_called()

    def test_setup_logging_uses_debug_console_level_when_debug_mode_enabled(self):
        with patch.dict(environ, {"DEBUG_MODE": "1"}, clear=False):
            config.setup_logging()
        console_handlers = [
            handler
            for handler in logging.getLogger().handlers
            if not isinstance(handler, logging.FileHandler)
        ]
        self.assertEqual(console_handlers[0].level, logging.DEBUG)

    def test_file_handlers_split_levels_without_duplicates(self):
        config.setup_logging(show_startup_banner=False)
        root = logging.getLogger()
        root.debug("debug-only-token")
        root.info("info-only-token")
        root.warning("warn-only-token")
        root.error("error-only-token")
        for handler in root.handlers:
            handler.flush()

        info_text = config.INFO_LOG_FILE.read_text(encoding="utf-8")
        warn_text = config.WARN_LOG_FILE.read_text(encoding="utf-8")
        error_text = config.ERROR_LOG_FILE.read_text(encoding="utf-8")

        self.assertIn("debug-only-token", info_text)
        self.assertIn("info-only-token", info_text)
        self.assertNotIn("warn-only-token", info_text)
        self.assertNotIn("error-only-token", info_text)
        self.assertIn("warn-only-token", warn_text)
        self.assertNotIn("info-only-token", warn_text)
        self.assertNotIn("error-only-token", warn_text)
        self.assertIn("error-only-token", error_text)
        self.assertNotIn("warn-only-token", error_text)

    def test_dated_log_namer_places_date_before_extension(self):
        default = str(config.LOGS_DIR / "app-error.log.2026-07-29")
        self.assertEqual(
            config._dated_log_namer(default),
            str(config.LOGS_DIR / "app-error-2026-07-29.log"),
        )

    def test_data_layout_does_not_migrate_legacy_flat_files(self):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        legacy_cookie = config.DATA_DIR / "cookies.json"
        legacy_log = config.DATA_DIR / "log.log"
        legacy_cookie.write_text("[]", encoding="utf-8")
        legacy_log.write_text("old log", encoding="utf-8")

        config.ensure_data_layout()

        self.assertTrue(legacy_cookie.exists())
        self.assertTrue(legacy_log.exists())
        self.assertFalse(config.COOKIES_FILE.exists())
        self.assertFalse(config.INFO_LOG_FILE.exists())
        self.assertTrue(config.REFERENCE_OUTPUT_DIR.is_dir())

    def test_build_console_handler_upgrades_console_streams_to_utf8_when_possible(self):
        class DummyStream:
            def __init__(self, encoding):
                self.encoding = encoding
                self.errors = "strict"

            def reconfigure(self, **kwargs):
                self.encoding = kwargs.get("encoding", self.encoding)
                self.errors = kwargs.get("errors", self.errors)

        class DummyRichHandler(logging.Handler):
            def __init__(self, *args, **kwargs):
                super().__init__()

        stdout = DummyStream("gbk")
        stderr = DummyStream("gbk")

        with (
            patch.object(config.sys, "stdout", stdout),
            patch.object(config.sys, "stderr", stderr),
            patch("rich.logging.RichHandler", DummyRichHandler),
        ):
            handler = config._build_console_handler()

        self.assertIsInstance(handler, DummyRichHandler)
        self.assertEqual(stdout.encoding, "utf-8")
        self.assertEqual(stderr.encoding, "utf-8")
        self.assertEqual(stdout.errors, "replace")
        self.assertEqual(stderr.errors, "replace")

    def test_build_console_handler_falls_back_to_plain_handler_when_console_stays_non_utf8(self):
        class DummyStream:
            def __init__(self, encoding):
                self.encoding = encoding
                self.errors = "strict"

            def reconfigure(self, **kwargs):
                self.errors = kwargs.get("errors", self.errors)

        class DummyRichHandler(logging.Handler):
            def __init__(self, *args, **kwargs):
                super().__init__()

        stdout = DummyStream("gbk")
        stderr = DummyStream("gbk")

        with (
            patch.object(config.sys, "stdout", stdout),
            patch.object(config.sys, "stderr", stderr),
            patch("rich.logging.RichHandler", DummyRichHandler),
        ):
            handler = config._build_console_handler()

        self.assertIsInstance(handler, logging.StreamHandler)
        self.assertNotIsInstance(handler, DummyRichHandler)
        self.assertEqual(stdout.encoding, "gbk")
        self.assertEqual(stderr.encoding, "gbk")
        self.assertEqual(stdout.errors, "replace")
        self.assertEqual(stderr.errors, "replace")

    def test_sanitize_console_message_removes_playwright_call_log_block(self):
        raw_message = (
            "Locator.wait_for: Timeout 3000ms exceeded.\n"
            "Call log:\n"
            '  - waiting for locator(".single-btns") to be visible\n'
            "  - element is visible\n"
            "\n"
            "后续普通日志"
        )

        cleaned = config._sanitize_console_message(raw_message)

        self.assertEqual(
            cleaned,
            "Locator.wait_for: Timeout 3000ms exceeded.\n后续普通日志",
        )

    def test_console_formatter_uses_sanitized_message(self):
        formatter = config._SanitizedConsoleFormatter("%(message)s")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=(
                "主日志\n"
                "Call log:\n"
                "  - step 1\n"
                "  - step 2\n"
                "下一行"
            ),
            args=(),
            exc_info=None,
        )

        rendered = formatter.format(record)

        self.assertEqual(rendered, "主日志\n下一行")

    def test_sanitize_console_message_drops_traceback_only_records(self):
        cleaned = config._sanitize_console_message(
            "Traceback (most recent call last):\n"
            '  File "x.py", line 1, in <module>\n'
            "RuntimeError: boom\n"
        )

        self.assertEqual(cleaned, "")

    def test_summarize_exception_message_falls_back_for_raw_playwright_locator_error(self):
        summarized = config.summarize_exception_message(
            RuntimeError(
                "Locator.wait_for: Timeout 3000ms exceeded.\n"
                "Call log:\n"
                '  - waiting for locator(".single-btns") to be visible\n'
            ),
            "处理失败",
        )

        self.assertEqual(summarized, "处理失败")

    def test_asyncio_exception_handler_suppresses_unretrieved_target_closed_call_log(self):
        class TargetClosedError(Exception):
            pass

        default_calls = []

        class FakeLoop:
            def default_exception_handler(self, context):
                default_calls.append(context)

        handler = config._make_asyncio_exception_handler(previous_handler=None)
        handler(
            FakeLoop(),
            {
                "message": "Future exception was never retrieved",
                "exception": TargetClosedError(
                    "Target page, context or browser has been closed\n"
                    "Call log:\n"
                    '  - navigating to "https://example.test", waiting until "load"\n'
                ),
            },
        )

        self.assertEqual(default_calls, [])

    def test_asyncio_exception_handler_keeps_unexpected_future_errors(self):
        previous_calls = []

        def previous_handler(loop, context):
            previous_calls.append((loop, context))

        loop = object()
        context = {
            "message": "Future exception was never retrieved",
            "exception": RuntimeError("boom"),
        }
        handler = config._make_asyncio_exception_handler(previous_handler)

        handler(loop, context)

        self.assertEqual(previous_calls, [(loop, context)])

    def test_run_async_keeps_target_closed_filter_during_runner_shutdown(self):
        class TargetClosedError(Exception):
            pass

        default_calls = []
        previous_calls = []

        def previous_handler(loop, context):
            previous_calls.append((loop, context))

        class FakeLoop:
            def __init__(self):
                self.handler = None

            def get_exception_handler(self):
                return previous_handler

            def set_exception_handler(self, handler):
                self.handler = handler

            def default_exception_handler(self, context):
                default_calls.append(context)

        class FakeRunner:
            def __init__(self):
                self.loop = FakeLoop()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.loop.handler(
                    self.loop,
                    {
                        "message": "Future exception was never retrieved",
                        "exception": TargetClosedError(
                            "Target page, context or browser has been closed"
                        ),
                    },
                )
                return False

            def get_loop(self):
                return self.loop

            def run(self, awaitable):
                awaitable.close()
                return "runner-result"

        async def noop():
            return "coroutine-result"

        with patch.object(config.asyncio, "Runner", return_value=FakeRunner()):
            result = config.run_async(noop())

        self.assertEqual(result, "runner-result")
        self.assertEqual(default_calls, [])
        self.assertEqual(previous_calls, [])

    def test_disable_windows_console_input_modes_clears_quick_edit_and_insert(self):
        class FakeKernel32:
            def __init__(self):
                self.mode = 0x0067
                self.set_mode = None

            def GetStdHandle(self, handle):
                self.handle = handle
                return 123

            def GetConsoleMode(self, handle, mode_ref):
                mode_ref._obj.value = self.mode
                return 1

            def SetConsoleMode(self, handle, mode):
                self.set_mode = mode
                return 1

        fake_kernel32 = FakeKernel32()
        fake_ctypes = type(
            "FakeCtypes",
            (),
            {
                "windll": type("FakeWindll", (), {"kernel32": fake_kernel32})(),
                "c_uint": config.ctypes.c_uint,
                "byref": config.ctypes.byref,
            },
        )()

        with (
            patch.object(config.sys, "platform", "win32"),
            patch.object(config, "ctypes", fake_ctypes),
        ):
            config._disable_windows_console_input_modes()

        self.assertEqual(fake_kernel32.handle, -10)
        self.assertIsNotNone(fake_kernel32.set_mode)
        self.assertEqual(fake_kernel32.set_mode & 0x0040, 0)
        self.assertEqual(fake_kernel32.set_mode & 0x0020, 0)
        self.assertNotEqual(fake_kernel32.set_mode & 0x0080, 0)


if __name__ == "__main__":
    unittest.main()
