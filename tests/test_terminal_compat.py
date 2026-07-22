"""core.ui.terminal_compat：收口后的终端识别（bat / launcher 入口）。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from core.ui.terminal_compat import (
    _ASCII_GLYPHS,
    _ASCII_PROGRESS,
    _UNICODE_GLYPHS,
    _UNICODE_PROGRESS,
    clear_terminal_compat_cache,
    detect_terminal_kind,
    is_legacy_windows_console,
    prefers_unicode_ui,
    progress_charset,
    ui_glyphs,
)


def _drop_terminal_env() -> None:
    for key in (
        "WT_SESSION",
        "WT_PROFILE_ID",
        "TERM_PROGRAM",
        "VSCODE_INJECTION",
        "COURSE_AFK_UI_CHARS",
    ):
        os.environ.pop(key, None)


class TerminalCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_terminal_compat_cache()

    def tearDown(self) -> None:
        clear_terminal_compat_cache()

    def test_force_unicode_via_env(self) -> None:
        with patch.dict(os.environ, {"COURSE_AFK_UI_CHARS": "unicode"}, clear=False):
            self.assertTrue(prefers_unicode_ui())
            self.assertEqual(progress_charset().name, "unicode")
            self.assertEqual(detect_terminal_kind(), "forced_unicode")

    def test_force_ascii_via_env(self) -> None:
        with patch.dict(os.environ, {"COURSE_AFK_UI_CHARS": "ascii"}, clear=False):
            self.assertFalse(prefers_unicode_ui())
            self.assertEqual(progress_charset().name, "ascii")
            self.assertEqual(detect_terminal_kind(), "forced_ascii")

    def test_wt_session_prefers_unicode(self) -> None:
        with patch.dict(os.environ, {"WT_SESSION": "abc-123"}, clear=False):
            os.environ.pop("COURSE_AFK_UI_CHARS", None)
            with patch("core.ui.terminal_compat.is_windows", return_value=True):
                with patch(
                    "core.ui.terminal_compat._console_window_class",
                    return_value="",
                ):
                    with patch(
                        "core.ui.terminal_compat._windows_ancestor_stems",
                        return_value=("cmd",),
                    ):
                        self.assertTrue(prefers_unicode_ui())
                        self.assertEqual(detect_terminal_kind(), "windows_terminal")

    def test_pure_conhost_prefers_ascii(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            _drop_terminal_env()
            with patch("core.ui.terminal_compat.is_windows", return_value=True):
                with patch(
                    "core.ui.terminal_compat._windows_ancestor_stems",
                    return_value=("cmd", "explorer"),
                ):
                    with patch(
                        "core.ui.terminal_compat._console_window_class",
                        return_value="ConsoleWindowClass",
                    ):
                        self.assertTrue(is_legacy_windows_console())
                        self.assertFalse(prefers_unicode_ui())
                        self.assertEqual(detect_terminal_kind(), "conhost")
                        self.assertEqual(progress_charset().name, "ascii")

    def test_win11_bat_via_pseudoconsole(self) -> None:
        """双击 bat：无 WT_SESSION、祖先无 WT，靠 PseudoConsoleWindow。"""
        with patch.dict(os.environ, {}, clear=False):
            _drop_terminal_env()
            with patch("core.ui.terminal_compat.is_windows", return_value=True):
                with patch(
                    "core.ui.terminal_compat._windows_ancestor_stems",
                    return_value=("cmd", "explorer"),
                ):
                    with patch(
                        "core.ui.terminal_compat._console_window_class",
                        return_value="PseudoConsoleWindow",
                    ):
                        self.assertFalse(is_legacy_windows_console())
                        self.assertTrue(prefers_unicode_ui())
                        self.assertEqual(
                            detect_terminal_kind(),
                            "console_class:PseudoConsoleWindow",
                        )
                        self.assertEqual(ui_glyphs().name, "unicode")

    def test_ancestor_wt_fallback(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            _drop_terminal_env()
            with patch("core.ui.terminal_compat.is_windows", return_value=True):
                with patch(
                    "core.ui.terminal_compat._console_window_class",
                    return_value="",
                ):
                    with patch(
                        "core.ui.terminal_compat._windows_ancestor_stems",
                        return_value=("cmd", "openconsole"),
                    ):
                        self.assertTrue(prefers_unicode_ui())
                        self.assertEqual(detect_terminal_kind(), "ancestor:openconsole")

    def test_vscode_dev_prefers_unicode(self) -> None:
        with patch.dict(os.environ, {"TERM_PROGRAM": "vscode"}, clear=False):
            _drop_terminal_env()
            os.environ["TERM_PROGRAM"] = "vscode"
            with patch("core.ui.terminal_compat.is_windows", return_value=True):
                with patch(
                    "core.ui.terminal_compat._console_window_class",
                    return_value="",
                ):
                    with patch(
                        "core.ui.terminal_compat._windows_ancestor_stems",
                        return_value=("cmd",),
                    ):
                        self.assertTrue(prefers_unicode_ui())
                        self.assertEqual(detect_terminal_kind(), "vscode")

    def test_parent_cmd_alone_is_not_enough_for_unicode(self) -> None:
        """父进程是 cmd 不能当现代终端证据（也不单独当 legacy 以外的信号）。"""
        with patch.dict(os.environ, {}, clear=False):
            _drop_terminal_env()
            with patch("core.ui.terminal_compat.is_windows", return_value=True):
                with patch(
                    "core.ui.terminal_compat._console_window_class",
                    return_value="",
                ):
                    with patch(
                        "core.ui.terminal_compat._windows_ancestor_stems",
                        return_value=("cmd", "explorer"),
                    ):
                        self.assertTrue(is_legacy_windows_console())
                        self.assertEqual(detect_terminal_kind(), "cmd")

    def test_explicit_unicode_override_on_charset(self) -> None:
        self.assertIs(progress_charset(unicode=True), _UNICODE_PROGRESS)
        self.assertIs(progress_charset(unicode=False), _ASCII_PROGRESS)

    def test_unicode_charset_has_braille_and_box(self) -> None:
        cs = _UNICODE_PROGRESS
        self.assertIn("⠋", cs.spinner)
        self.assertEqual(cs.track_done, "━")
        self.assertEqual(cs.bracket_l, "")

    def test_ascii_charset_all_single_width(self) -> None:
        cs = _ASCII_PROGRESS
        for ch in cs.spinner + (cs.track_done, cs.track_todo, cs.bracket_l, cs.bracket_r):
            self.assertEqual(len(ch), 1)
            self.assertTrue(all(ord(c) < 128 for c in ch))

    def test_unicode_glyphs_pretty(self) -> None:
        g = _UNICODE_GLYPHS
        self.assertEqual(g.icon_success, "✓")
        self.assertEqual(g.arrow, "→")
        self.assertEqual(g.textual_border, "round")

    def test_ascii_glyphs_cmd_safe(self) -> None:
        g = _ASCII_GLYPHS
        self.assertEqual(g.icon_success, "[+]")
        self.assertEqual(g.arrow, "->")
        for icon in (g.icon_info, g.icon_success, g.icon_warning, g.icon_failure):
            self.assertTrue(all(ord(c) < 128 for c in icon))


if __name__ == "__main__":
    unittest.main()
