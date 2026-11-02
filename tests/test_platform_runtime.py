import asyncio
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from core.runtime import application_lock
from core import config


class NativePlatformTests(unittest.TestCase):
    def test_native_launcher_uses_its_own_directory_and_preserves_exit_status(self):
        import shutil
        import json

        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "项目 space"
            root.mkdir()
            capture = root / "arguments.json"
            project = Path(__file__).resolve().parents[1]
            env = {**os.environ, "LAUNCH_CAPTURE": str(capture)}
            if os.name == "nt":
                shutil.copyfile(project / "run.bat", root / "run.bat")
                (root / "launcher.py").write_text(
                    "import os,sys,json;from pathlib import Path;Path(os.environ['LAUNCH_CAPTURE']).write_text(json.dumps(sys.argv));sys.exit(17)",
                    encoding="utf-8",
                )
                env["PATH"] = (
                    str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
                )
                result = subprocess.run(
                    f'cmd.exe /d /s /c ""{root / "run.bat"}" "argument with spaces""',
                    cwd=tmp,
                    env=env,
                    input="\n",
                    text=True,
                    capture_output=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
                self.assertEqual(
                    json.loads(capture.read_text())[1:], ["argument with spaces"]
                )
            else:
                shutil.copyfile(project / "run.command", root / "run.command")
                binary = root / ".venv/bin/python"
                binary.parent.mkdir(parents=True)
                binary.write_text(
                    '#!/bin/sh\nprintf "%s\\n" "$@" > "$LAUNCH_CAPTURE"\nexit 17\n',
                    encoding="utf-8",
                )
                binary.chmod(0o755)
                result = subprocess.run(
                    ["/bin/sh", str(root / "run.command"), "argument with spaces"],
                    cwd=tmp,
                    env=env,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 17, result.stderr.decode())
                self.assertEqual(
                    capture.read_text().splitlines(),
                    ["-X", "utf8", str(root / "launcher.py"), "argument with spaces"],
                )

    def test_single_instance_lock_with_unicode_space_and_long_path(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ("中文 space " + ("long" * 30)) / "application.lock"
            code = "from pathlib import Path\nimport sys\nfrom core.runtime import application_lock\ntry:\n with application_lock(Path(sys.argv[1])): pass\nexcept RuntimeError: sys.exit(23)\n"

            def acquire():
                return subprocess.run(
                    [sys.executable, "-c", code, str(path)],
                    capture_output=True,
                    timeout=10,
                )

            with application_lock(path):
                self.assertEqual(acquire().returncode, 23)
            self.assertEqual(acquire().returncode, 0)

    def test_explicit_empty_browser_channel_and_macos_brand_preference(self):
        with (
            patch.object(config.sys, "platform", "darwin"),
            patch.object(Path, "exists", return_value=False),
        ):
            self.assertEqual(config._default_browser_channel("chromium"), "chrome")
        with (
            patch.object(config.sys, "platform", "darwin"),
            patch.object(Path, "exists", side_effect=lambda: True),
        ):
            self.assertEqual(config._default_browser_channel("chromium"), "chrome")
        from core.browser.session import build_browser_launch_options

        with patch("core.browser.session.BROWSER_CHANNEL", None):
            self.assertNotIn("channel", build_browser_launch_options(headless=False))

    @unittest.skipIf(
        os.name == "nt",
        "POSIX signal behavior; Windows cancellation is tested separately",
    )
    def test_sigterm_cancels_and_drains_the_application_loop(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = """import asyncio,signal,sys
from pathlib import Path
from core.runtime import run_operation,interrupt_running_async
from core.workflow.host import shutdown_host
root=Path(sys.argv[1])
signal.signal(signal.SIGTERM,lambda *_:interrupt_running_async(include_current=True))
async def work():
 root.joinpath('ready').touch()
 try: await asyncio.sleep(300)
 finally:
  await asyncio.sleep(.03)
  root.joinpath('cleaned').touch()
try: run_operation(work())
except asyncio.CancelledError: pass
finally: shutdown_host()
"""
            env = {**os.environ, "COURSE_AFK_DATA_DIR": str(root / "data")}
            process = subprocess.Popen(
                [sys.executable, "-c", code, str(root)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                import time

                deadline = time.monotonic() + 15
                while (
                    not (root / "ready").exists()
                    and time.monotonic() < deadline
                    and process.poll() is None
                ):
                    time.sleep(0.02)
                self.assertTrue((root / "ready").exists())
                process.terminate()
                out, err = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, err.decode(errors="replace"))
                self.assertTrue((root / "cleaned").exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)


@unittest.skipIf(
    os.name == "nt", "POSIX transport ownership; Windows uses Job Object integration"
)
class PosixOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_descendants_are_reaped_and_unrelated_process_survives(self):
        from core.runtime import PosixDriver
        import psutil

        unrelated = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time;time.sleep(30)"
        )
        parent = None
        try:
            with TemporaryDirectory() as tmp:
                pidfile = Path(tmp) / "child.pid"
                script = "import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);Path(sys.argv[1]).write_text(str(p.pid));time.sleep(30)"
                parent = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", script, str(pidfile)
                )
                async with asyncio.timeout(5):
                    while not pidfile.exists():
                        await asyncio.sleep(0.02)
                child = psutil.Process(int(pidfile.read_text()))
                owner = PosixDriver(parent)
                owner.capture()
                await owner.terminate()
                self.assertFalse(
                    child.is_running() and child.status() != psutil.STATUS_ZOMBIE
                )
                self.assertIsNone(unrelated.returncode)
        finally:
            for process in (parent, unrelated):
                if process is not None and process.returncode is None:
                    process.kill()
                    await process.wait()
