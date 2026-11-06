"""Run frontend action checks using Playwright's bundled Node, without Edge."""
from pathlib import Path
import subprocess
import unittest

from playwright._impl._driver import compute_driver_executable


class WebActionTests(unittest.TestCase):
    def test_recommendation_clicks_share_operation_options_and_guards(self):
        node, _ = compute_driver_executable()
        result = subprocess.run([str(node), str(Path(__file__).with_name("web_actions.cjs"))],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
