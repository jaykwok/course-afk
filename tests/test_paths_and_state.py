import importlib
import sys
import unittest

from core import config


class PathConfigTests(unittest.TestCase):
    def test_default_logs_are_project_relative_even_with_custom_data_root(self):
        import os
        import subprocess
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as tmp:
            env = {**os.environ, "COURSE_AFK_DATA_DIR": tmp}
            env.pop("COURSE_AFK_LOGS_DIR", None)
            code = ("from unittest.mock import patch; "
                    "p=patch('dotenv.dotenv_values',return_value={}); p.start(); "
                    "from core import config; print(config.LOGS_DIR)")
            result = subprocess.run([sys.executable, "-c", code], env=env,
                                    cwd=config.PROJECT_ROOT, capture_output=True,
                                    text=True, encoding="utf-8", timeout=20, check=True)
            self.assertEqual(Path(result.stdout.strip()), config.PROJECT_ROOT / "logs")

    def test_business_data_and_logs_use_independent_roots(self):
        import os
        from pathlib import Path
        self.assertEqual(config.DATA_DIR, Path(os.environ["COURSE_AFK_DATA_DIR"]).resolve())
        self.assertEqual(config.LOGS_DIR, Path(os.environ["COURSE_AFK_LOGS_DIR"]).resolve())
        self.assertEqual(config.LEARNING_URLS_FILE.parent, config.LINKS_DIR)
        self.assertEqual(config.LEARNING_FAILURES_FILE.parent, config.LINKS_DIR)
        self.assertEqual(config.EXAM_URLS_FILE.parent, config.LINKS_DIR)
        self.assertEqual(config.MANUAL_EXAM_FILE.parent, config.LINKS_DIR)
        self.assertEqual(config.COOKIES_FILE.parent, config.CREDENTIALS_DIR)
        self.assertEqual(config.INFO_LOG_FILE.parent, config.LOGS_DIR)
        self.assertEqual(config.WARN_LOG_FILE.parent, config.LOGS_DIR)
        self.assertEqual(config.ERROR_LOG_FILE.parent, config.LOGS_DIR)
        self.assertEqual(config.REFERENCE_OUTPUT_DIR.parent, config.DATA_DIR)
        # 断兼容：路径常量不得回落到项目根
        self.assertNotEqual(config.LEARNING_URLS_FILE, config.PROJECT_ROOT / "课程链接.json")
        self.assertNotEqual(config.COOKIES_FILE, config.PROJECT_ROOT / "cookies.json")
        self.assertEqual(config.INFO_LOG_FILE.name, "app-info.log")
        self.assertEqual(config.WARN_LOG_FILE.name, "app-warn.log")
        self.assertEqual(config.ERROR_LOG_FILE.name, "app-error.log")
        self.assertFalse(hasattr(config, "LOG_FILE"))

    def test_learning_links_file_is_project_relative(self):
        self.assertTrue(str(config.LEARNING_URLS_FILE).endswith("课程链接.json"))

    def test_learning_failures_file_is_project_relative(self):
        self.assertTrue(str(config.LEARNING_FAILURES_FILE).endswith("挂课失败链接.json"))

    def test_exam_links_file_is_project_relative(self):
        self.assertTrue(str(config.EXAM_URLS_FILE).endswith("考试链接.json"))


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
