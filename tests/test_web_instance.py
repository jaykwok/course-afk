"""Real app lock/reopen/owned-process cleanup using a credential-free data root."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import psutil

from core.web.server import reopen_existing


class WebInstanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeat_launch_focuses_same_instance_and_exit_reaps_owned_children(self):
        root=Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as tmp:
            directory=Path(tmp)
            env={**os.environ,"COURSE_AFK_DATA_DIR":tmp,"PYTHONUTF8":"1","SUPPRESS_STARTUP_BANNER":"1"}
            with (directory/"worker.log").open("w",encoding="utf-8") as log:
                worker=subprocess.Popen([sys.executable,"-X","utf8",str(root/"tests/web_app_worker.py")],cwd=root,env=env,stdout=log,stderr=log)
                owned=[]
                app_process=None
                try:
                    async with asyncio.timeout(45):
                        while not (directory/"web-instance.json").exists():
                            if worker.poll() is not None:
                                self.fail((directory/"worker.log").read_text(encoding="utf-8")[-5000:])
                            await asyncio.sleep(.1)
                    metadata=json.loads((directory/"web-instance.json").read_text(encoding="utf-8"))
                    parent=psutil.Process(worker.pid)
                    descendants=parent.children(recursive=True)
                    self.assertIn(metadata["pid"],[worker.pid]+[p.pid for p in descendants])
                    app_process=psutil.Process(metadata["pid"])
                    self.assertEqual(app_process.create_time(),metadata["created"])
                    owned=app_process.children(recursive=True)
                    self.assertTrue(owned)
                    duplicate=await asyncio.create_subprocess_exec(sys.executable,"-X","utf8",str(root/"launcher.py"),cwd=root,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
                    output,_=await asyncio.wait_for(duplicate.communicate(),15)
                    self.assertEqual(duplicate.returncode,0,output.decode("utf-8",errors="replace"))
                    self.assertIsNone(worker.poll())
                    self.assertEqual(json.loads((directory/"web-instance.json").read_text(encoding="utf-8"))["instance_id"],metadata["instance_id"])
                    self.assertFalse((directory/"workflows/execution.sqlite").exists(),"startup/reopen must not create a DBOS engine")
                    (directory/"exit.flag").touch()
                    async with asyncio.timeout(40):
                        while worker.poll() is None:await asyncio.sleep(.1)
                    self.assertEqual(worker.returncode,0,(directory/"worker.log").read_text(encoding="utf-8")[-5000:])
                    self.assertFalse((directory/"web-instance.json").exists())
                    async with asyncio.timeout(5):
                        while any(p.is_running() for p in owned):await asyncio.sleep(.05)
                finally:
                    (directory/"exit.flag").touch()
                    if app_process and app_process.is_running():
                        try:await asyncio.to_thread(app_process.wait,10)
                        except psutil.TimeoutExpired:
                            app_process.terminate()
                            await asyncio.to_thread(app_process.wait,10)
                    if worker.poll() is None:
                        try:await asyncio.to_thread(worker.wait,5)
                        except subprocess.TimeoutExpired:
                            worker.terminate()
                            await asyncio.to_thread(worker.wait,10)

    def test_stale_or_nonloopback_metadata_does_not_open_a_browser(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/"instance.json"
            for content in ({"origin":"https://example.invalid"},{"origin":"http://127.0.0.1:1","pid":os.getpid(),"created":0}):
                path.write_text(json.dumps(content),encoding="utf-8")
                self.assertFalse(reopen_existing(path))
