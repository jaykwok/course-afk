import unittest

from launcher import MANUAL_SELECTION_PROMPTS, MENU_OPTIONS


class LauncherMenuTests(unittest.TestCase):
    def test_menu_matches_expected_labels_and_order(self):
        self.assertEqual(
            MENU_OPTIONS,
            [
                "推荐流程 / 继续上次进度（挂课+考试）",
                "仅挂课",
                "切换账号 / 更新登录凭证",
                "手动选择课程 / 录入课程或考试链接",
                "AI 自动考试",
                "人工考试",
                "查看输出文件统计",
                "查看课程链接详情",
                "退出",
            ],
        )

    def test_manual_selection_prompts_cover_signup_then_learning(self):
        joined = "\n".join(MANUAL_SELECTION_PROMPTS)
        self.assertIn("请粘贴入口链接", joined)
        self.assertIn("考试链接", joined)
        self.assertIn("学习专区链接", joined)
        self.assertIn("如页面提示需要报名", joined)
        self.assertIn("课程链接.json", joined)
        self.assertIn("考试链接.json", joined)


if __name__ == "__main__":
    unittest.main()
