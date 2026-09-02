"""tui_render 渲染层测试：账号胶囊的状态文字与超宽截断。

规则（见模块 docstring）：健康度不允许只靠状态点颜色表达，必须伴随文字；
账号名超宽时截断，状态字保持完整可见。
"""

from __future__ import annotations

import unittest

from rich.cells import cell_len

from core.auth.credential import CredentialMetadata
from core.state import ProjectState
from core.ui import tui_render


def _metadata(label: str = "张三（zhangsan）") -> CredentialMetadata:
    # expires_at 用远期日期：validity_parts 读当前时间，近期日期会让测试在
    # 到期当天起翻成「已过期」而失败（2099 年前不会触发）。
    return CredentialMetadata(
        saved_at="2026-07-01T10:00:00",
        expires_at="2099-12-31T10:00:00",
        account_display_name="张三",
        account_name="zhangsan",
        account_label=label,
    )


class AccountChipTests(unittest.TestCase):
    def test_expired_chip_contains_text_status_not_color_only(self):
        """凭证过期：胶囊必须带「已过期」文字，不能只靠黄色状态点。"""
        state = ProjectState(
            has_credential=True,
            credential_expired=True,
            learning_count=0,
            learning_failure_count=0,
            exam_count=0,
            manual_exam_count=0,
        )
        chip = tui_render.build_account_chip(state, _metadata())
        self.assertIn("已过期", chip.plain)
        self.assertTrue(chip.plain.rstrip().endswith("已过期"))

    def test_valid_chip_contains_valid_text(self):
        state = ProjectState(True, False, 1, 0, 0, 0)
        chip = tui_render.build_account_chip(state, _metadata())
        self.assertIn("有效", chip.plain)
        self.assertIn("张三（zhangsan）", chip.plain)

    def test_missing_credential_chip_says_not_logged_in(self):
        state = ProjectState(False, True, 0, 0, 0, 0)
        chip = tui_render.build_account_chip(state, None)
        self.assertIn("未登录", chip.plain)

    def test_long_label_is_truncated_but_status_stays_visible(self):
        """超长账号名按显示宽度截断加省略号，状态字完整保留在末尾。"""
        state = ProjectState(True, False, 1, 0, 0, 0)
        long_label = "a" * 61
        chip = tui_render.build_account_chip(state, _metadata(label=long_label))
        self.assertIn("…", chip.plain)
        self.assertIn("有效", chip.plain)
        self.assertTrue(chip.plain.endswith("有效"))
        # 点(2) + 名字(≤20) + 分隔(3) + 状态字，总宽有界，不会越过品牌栏右缘
        self.assertLessEqual(cell_len(chip.plain), 2 + 20 + 3 + cell_len("有效"))

    def test_truncate_cells_is_cjk_aware(self):
        """CJK 截断按显示宽度（2 列/字）而不是字符数，结果不超上限。"""
        from core.ui.tui_render import _truncate_cells

        self.assertEqual(_truncate_cells("abcdefghij", 20), "abcdefghij")
        truncated = _truncate_cells("郭" * 30, 20)
        self.assertLessEqual(cell_len(truncated), 20)
        self.assertGreaterEqual(cell_len(truncated), 19)  # 省略号占 1 列
        self.assertTrue(truncated.endswith("…"))


if __name__ == "__main__":
    unittest.main()
