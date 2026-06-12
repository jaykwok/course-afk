import importlib
import sys
import unittest

from core import config


class PathConfigTests(unittest.TestCase):
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

    def test_core_package_does_not_eagerly_import_learning_module(self):
        import core

        sys.modules.pop("core.learning", None)
        importlib.reload(core)

        self.assertNotIn("core.learning", sys.modules)

    def test_launcher_controller_does_not_eagerly_import_workflows(self):
        import core.launcher_controller as launcher_controller

        sys.modules.pop("core.workflows", None)
        sys.modules.pop("core.learning", None)
        importlib.reload(launcher_controller)

        self.assertNotIn("core.workflows", sys.modules)
        self.assertNotIn("core.learning", sys.modules)


if __name__ == "__main__":
    unittest.main()
