"""Offline launcher lifecycle worker. Never starts a platform operation."""
from pathlib import Path
import os
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.app import runtime

OriginalRuntime=runtime.ApplicationRuntime


class TestRuntime(OriginalRuntime):
    def tick(self):
        if (Path(os.environ["COURSE_AFK_DATA_DIR"])/"exit.flag").exists():
            self.request_exit()
        return super().tick()


runtime.ApplicationRuntime=TestRuntime
import launcher
raise SystemExit(launcher.entrypoint())
