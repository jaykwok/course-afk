"""core.ui.terminal_compat：终端识别与进度条字符集策略。"""

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
        "ConEmuANSI",
        "ConEmuPID",
        "CMDER_ROOT",
        "MSYSTEM",
        "MINGW_PREFIX",
        "TERMINAL_EMULATOR",
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

    def test_windows_terminal_prefers_unicode(self) -> None:
        env = {
            "WT_SESSION": "abc-123",
            "COURSE_AFK_UI_CHARS": "",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("COURSE_AFK_UI_CHARS", None)
            with patch("core.ui.terminal_compat.is_windows", return_value=True):
                with patch(
                    "core.ui.terminal_compat._windows_ancestor_stems",
                    return_value=("cmd",),
                ):
                    self.assertTrue(prefers_unicode_ui())
                    self.assertFalse(is_legacy_windows_console())
                    self.assertEqual(detect_terminal_kind(), "windows_terminal")

    def test_pure_cmd_prefers_ascii(self) -> None:
        # 模拟真·纯 conhost：祖先只有 cmd / explorer，无 OpenConsole/WT
        with patch.dict(os.environ, {}, clear=False):
            _drop_terminal_env()
            with patch("core.ui.terminal_compat.is_windows", return_value=True):
                with patch(
                    "core.ui.terminal_compat._windows_ancestor_stems",
                    return_value=("cmd", "explorer"),
                ):
                    self.assertTrue(is_legacy_windows_console())
                    self.assertFalse(prefers_unicode_ui())
                    self.assertEqual(detect_terminal_kind(), "cmd")
                    cs = progress_charset()
                    self.assertEqual(cs.name, "ascii")
                    self.assertEqual(cs.track_done, "#")
                    self.assertEqual(cs.bracket_l, "[")

    def test_win11_default_terminal_bat_chain_prefers_unicode(self) -> None:
        """Win11 默认终端=WT：双击 bat 时 python←cmd←OpenConsole←WindowsTerminal，
        且通常不注入 WT_SESSION。"""
        with patch.dict(os.environ, {}, clear=False):
            _drop_terminal_env()
            with patch("core.ui.terminal_compat.is_windows", return_value=True):
                with patch(
                    "core.ui.terminal_compat._windows_ancestor_stems",
                    return_value=("cmd", "openconsole", "windowsterminal"),
                ):
                    self.assertFalse(is_legacy_windows_console())
                    self.assertTrue(prefers_unicode_ui())
                    self.assertEqual(detect_terminal_kind(), "ancestor:openconsole")
                    self.assertEqual(ui_glyphs().name, "unicode")

    def test_openconsole_grandparent_alone_prefers_unicode(self) -> None:
        """仅祖先里有 OpenConsole（Win11 默认终端托管）也应走 Unicode。"""
        with patch.dict(os.environ, {}, clear=False):
            _drop_terminal_env()
            with patch("core.ui.terminal_compat.is_windows", return_value=True):
                with patch(
                    "core.ui.terminal_compat._windows_ancestor_stems",
                    return_value=("cmd", "openconsole"),
                ):
                    self.assertTrue(prefers_unicode_ui())
                    self.assertEqual(detect_terminal_kind(), "ancestor:openconsole")

    def test_direct_parent_windows_terminal_prefers_unicode(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            _drop_terminal_env()
            with patch("core.ui.terminal_compat.is_windows", return_value=True):
                with patch(
                    "core.ui.terminal_compat._windows_ancestor_stems",
                    return_value=("windowsterminal",),
                ):
                    self.assertFalse(is_legacy_windows_console())
                    self.assertTrue(prefers_unicode_ui())
                    self.assertEqual(detect_terminal_kind(), "ancestor:windowsterminal")

    def test_explicit_unicode_override_on_charset(self) -> None:
        self.assertIs(progress_charset(unicode=True), _UNICODE_PROGRESS)
        self.assertIs(progress_charset(unicode=False), _ASCII_PROGRESS)

    def test_unicode_charset_has_braille_and_box(self) -> None:
        cs = _UNICODE_PROGRESS
        self.assertIn("⠋", cs.spinner)
        self.assertEqual(cs.track_done, "━")
        self.assertEqual(cs.track_todo, "─")
        self.assertEqual(cs.bracket_l, "")

    def test_ascii_charset_all_single_width(self) -> None:
        cs = _ASCII_PROGRESS
        for ch in cs.spinner + (cs.track_done, cs.track_todo, cs.bracket_l, cs.bracket_r):
            self.assertEqual(len(ch), 1)
            self.assertTrue(all(ord(c) < 128 for c in ch))

    def test_unicode_glyphs_pretty_icons_and_arrow(self) -> None:
        g = _UNICODE_GLYPHS
        self.assertEqual(g.icon_success, "✓")
        self.assertEqual(g.icon_failure, "✗")
        self.assertEqual(g.arrow, "→")
        self.assertEqual(g.textual_border, "round")
        self.assertIn("·", g.join_keys("A", "B"))

    def test_ascii_glyphs_cmd_safe(self) -> None:
        g = _ASCII_GLYPHS
        self.assertEqual(g.icon_success, "[+]")
        self.assertEqual(g.icon_failure, "[-]")
        self.assertEqual(g.arrow, "->")
        self.assertEqual(g.textual_border, "ascii")
        for icon in (g.icon_info, g.icon_success, g.icon_warning, g.icon_failure):
            self.assertTrue(all(ord(c) < 128 for c in icon))

    def test_ui_glyphs_respects_force_env(self) -> None:
        with patch.dict(os.environ, {"COURSE_AFK_UI_CHARS": "unicode"}, clear=False):
            self.assertEqual(ui_glyphs().name, "unicode")
        with patch.dict(os.environ, {"COURSE_AFK_UI_CHARS": "ascii"}, clear=False):
            self.assertEqual(ui_glyphs().name, "ascii")


if __name__ == "__main__":
    unittest.main()
