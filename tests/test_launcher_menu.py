import unittest

from core.menu_keys import MAX_MENU_OPTIONS, menu_key_for_index, parse_menu_key
from launcher import MANUAL_SELECTION_PROMPTS, MENU_OPTIONS


class LauncherMenuTests(unittest.TestCase):
    def test_menu_matches_expected_labels_and_order(self):
        self.assertEqual(
            MENU_OPTIONS,
            [
                "开始挂课",
                "AI 考试",
                "添加课程 / 考试链接",
                "课程资料",
                "任务管理",
                "账号与登录",
                "退出",
            ],
        )

    def test_main_menu_fits_single_page_with_zero_for_exit(self):
        """主菜单保持六组操作，退出始终为 0。"""
        self.assertLessEqual(len(MENU_OPTIONS), MAX_MENU_OPTIONS)
        self.assertEqual(len(MENU_OPTIONS), 7)
        self.assertEqual(MENU_OPTIONS[-1], "退出")
        self.assertEqual(menu_key_for_index(7, 7, zero_for_last=True), "0")
        self.assertEqual(parse_menu_key("0", 7, zero_for_last=True), 7)

    def test_manual_selection_prompts_cover_signup_then_learning(self):
        joined = "\n".join(MANUAL_SELECTION_PROMPTS)
        self.assertIn("请粘贴入口链接", joined)
        self.assertIn("考试链接", joined)
        self.assertIn("课程集合页（学习专区 / 案例库）", joined)
        self.assertIn("如页面提示需要报名", joined)
        self.assertIn("课程链接.json", joined)
        self.assertIn("考试链接.json", joined)


if __name__ == "__main__":
    unittest.main()
