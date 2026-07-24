import importlib
import sys
import unittest

from core import config


class PathConfigTests(unittest.TestCase):
    def test_runtime_files_live_under_data_dir(self):
        self.assertEqual(config.DATA_DIR, config.PROJECT_ROOT / "data")
        self.assertEqual(config.LEARNING_URLS_FILE.parent, config.DATA_DIR)
        self.assertEqual(config.LEARNING_FAILURES_FILE.parent, config.DATA_DIR)
        self.assertEqual(config.EXAM_URLS_FILE.parent, config.DATA_DIR)
        self.assertEqual(config.MANUAL_EXAM_FILE.parent, config.DATA_DIR)
        self.assertEqual(config.COOKIES_FILE.parent, config.DATA_DIR)
        self.assertEqual(config.CREDENTIAL_META_FILE.parent, config.DATA_DIR)
        self.assertEqual(config.LOG_FILE.parent, config.DATA_DIR)
        self.assertEqual(config.REFERENCE_OUTPUT_DIR.parent, config.DATA_DIR)
        # 断兼容：路径常量不得回落到项目根
        self.assertNotEqual(config.LEARNING_URLS_FILE, config.PROJECT_ROOT / "课程链接.json")
        self.assertNotEqual(config.COOKIES_FILE, config.PROJECT_ROOT / "cookies.json")
        self.assertEqual(config.LOG_FILE, config.DATA_DIR / "log.log")
        self.assertNotEqual(config.LOG_FILE, config.PROJECT_ROOT / "log.txt")
        self.assertNotEqual(config.LOG_FILE, config.PROJECT_ROOT / "log.log")

    def test_learning_links_file_is_project_relative(self):
        self.assertTrue(str(config.LEARNING_URLS_FILE).endswith("课程链接.json"))

    def test_learning_failures_file_is_project_relative(self):
        self.assertTrue(str(config.LEARNING_FAILURES_FILE).endswith("挂课失败链接.json"))

    def test_exam_links_file_is_project_relative(self):
        self.assertTrue(str(config.EXAM_URLS_FILE).endswith("考试链接.json"))

    def test_cookie_metadata_file_is_defined(self):
        self.assertTrue(
            str(config.CREDENTIAL_META_FILE).endswith("credential_meta.json")
        )

    def test_core_package_does_not_eagerly_import_learning_flows(self):
        import core

        sys.modules.pop("core.learning.flows", None)
        importlib.reload(core)

        self.assertNotIn("core.learning.flows", sys.modules)

    def test_launcher_controller_does_not_eagerly_import_workflows(self):
        import core.app.launcher_controller as launcher_controller

        sys.modules.pop("core.app.workflows", None)
        sys.modules.pop("core.learning.flows", None)
        importlib.reload(launcher_controller)

        self.assertNotIn("core.app.workflows", sys.modules)
        self.assertNotIn("core.learning.flows", sys.modules)


if __name__ == "__main__":
    unittest.main()
