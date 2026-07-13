import unittest
from pathlib import Path

from core import config


class DistributionFilesTests(unittest.TestCase):
    def test_env_example_exists_and_uses_placeholder_values(self):
        env_example = config.PROJECT_ROOT / ".env.example"
        self.assertTrue(env_example.exists())

        content = env_example.read_text(encoding="utf-8")
        self.assertIn("OPENAI_COMPLETION_BASE_URL=", content)
        self.assertIn("OPENAI_COMPLETION_API_KEY=your_api_key_here", content)
        self.assertNotIn("DASHSCOPE_", content)
        self.assertIn("MODEL_NAME=qwen3.6-plus", content)
        self.assertIn("AI_REQUEST_TYPE=responses", content)
        self.assertIn("AI_ENABLE_WEB_SEARCH=1", content)
        self.assertIn("AI_ENABLE_THINKING=0", content)
        self.assertIn("AI_REASONING_EFFORT=medium", content)
        self.assertIn("默认关闭", content)
        self.assertIn("chat / responses", content)
        self.assertIn("联网搜索，默认关闭", content)
        self.assertIn("DEBUG_MODE=1", content)
        self.assertIn("SUPPRESS_STARTUP_BANNER=1", content)

    def test_run_bat_is_english_thin_launcher(self):
        run_bat = config.PROJECT_ROOT / "run.bat"
        content = run_bat.read_text(encoding="utf-8")

        self.assertIn("launcher.py", content)
        self.assertIn("title Course Automation", content)
        self.assertNotIn("mode con", content)
        self.assertIn("Course Automation", content)
        self.assertIn("set \"PYTHON_EXE=.venv\\Scripts\\python.exe\"", content)
        self.assertIn("if not exist \"%PYTHON_EXE%\" (", content)
        self.assertIn('python -c "import sys; sys.exit(0)" >nul 2>nul', content)
        self.assertIn("set \"PYTHON_EXE=python\"", content)
        self.assertIn("Python was not found", content)
        self.assertIn("Starting launcher.py", content)
        self.assertIn("Launcher exited with code", content)
        self.assertNotIn("WindowsPowerShell", content)
        self.assertNotIn("GetConsoleMode", content)
        self.assertNotIn("SetConsoleMode", content)
        self.assertNotIn("STARTUP_TRACE_FILE", content)
        self.assertNotIn("batch.before_python", content)
        self.assertNotIn("batch.after_python", content)

    def test_readme_recommends_bat_or_python_launcher(self):
        readme = (config.PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("run.bat", readme)
        self.assertIn("python launcher.py", readme)
        self.assertIn("OPENAI_COMPLETION_BASE_URL", readme)
        self.assertIn("OPENAI_COMPLETION_API_KEY", readme)
        self.assertNotIn("DASHSCOPE_", readme)
        self.assertIn("AI_REQUEST_TYPE=responses", readme)
        self.assertIn("chat", readme)
        self.assertIn("AI_ENABLE_THINKING=0", readme)
        self.assertIn("AI_REASONING_EFFORT=medium", readme)
        self.assertIn("课程链接.json", readme)
        self.assertIn("挂课失败链接.json", readme)
        self.assertIn("考试链接.json", readme)
        self.assertIn("人工考试链接.json", readme)
        self.assertIn("参考资料", readme)
        self.assertNotIn("课程链接.txt", readme)
        self.assertNotIn("剩余未看课程链接.txt", readme)
        self.assertNotIn("考试次数超限链接.txt", readme)
        self.assertNotIn("run.ps1", readme)

    def test_generated_workflow_outputs_are_json_only(self):
        gitignore = (config.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        ignored_lines = set(gitignore.splitlines())

        self.assertIn("课程链接.json", ignored_lines)
        self.assertIn("挂课失败链接.json", ignored_lines)
        self.assertIn("考试链接.json", ignored_lines)
        self.assertIn("人工考试链接.json", ignored_lines)
        self.assertNotIn("课程链接.txt", ignored_lines)
        self.assertNotIn("剩余未看课程链接.txt", ignored_lines)
        self.assertNotIn("考试次数超限链接.txt", ignored_lines)
        self.assertNotIn("无权限资源链接.txt", ignored_lines)
        self.assertNotIn("不合规链接.txt", ignored_lines)
        self.assertNotIn("URL类型链接.txt", ignored_lines)
        self.assertNotIn("h5课程类型链接.txt", ignored_lines)
        self.assertNotIn("调研类型链接.txt", ignored_lines)
        self.assertNotIn("未知类型链接.txt", ignored_lines)
        self.assertNotIn("非课程及考试类学习类型链接.txt", ignored_lines)
        self.assertNotIn("考试链接.txt", ignored_lines)
        self.assertNotIn("人工考试链接.txt", ignored_lines)


if __name__ == "__main__":
    unittest.main()
