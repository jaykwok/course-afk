"""Subprocess-only fault injection; synthetic effects and isolated databases."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODE, DIRECTORY = sys.argv[1], Path(sys.argv[2])
os.environ["COURSE_AFK_DATA_DIR"] = str(DIRECTORY)
from core.workflow.host import WorkflowHost
from core.workflow.contracts import TaskOutcome, Status
from core.tasks.identity import parse_entry

URL = "https://kc.zhixueyun.com/#/study/course/detail/11111111-1111-4111-8111-111111111111"


def event(value):
    with (DIRECTORY / "events.jsonl").open("a", encoding="utf-8") as out:
        out.write(json.dumps(value) + "\n")
        out.flush()
        os.fsync(out.fileno())


def exercise_progress_resume(host):
    import threading
    from types import SimpleNamespace
    from core.ui.frontend import use_frontend, wait_with_progress

    ready = threading.Event()
    attempts = 0

    async def progress_activity(host, payload):
        nonlocal attempts
        attempts += 1
        await wait_with_progress(0, "chapter")
        if attempts == 1 and MODE != "progress-recover":
            ready.set()
            try:
                await asyncio.sleep(300)
            finally:
                event("progress_cleanup")
        return TaskOutcome(payload["task_id"], Status.COMPLETED)

    def frontend(label):
        async def progress(duration, description):
            assert description == "chapter"
            event(label)
        return SimpleNamespace(wait_with_progress=progress)

    host.activities = {"learn": progress_activity}
    if MODE != "progress-recover":
        with use_frontend(frontend("first_frontend")):
            future = host.submit(host.execute(URL, "learn"))
        assert ready.wait(10), "activity did not start"
        run = host.repository.export()["runs"][0]
        host.submit(host.cancel(run["run_id"])).result(10)
        try:
            future.result(5)
        except Exception:
            pass
        assert not host.scopes
        if MODE == "progress-cancel":
            return
    with use_frontend(frontend("resumed_frontend")):
        result = host.submit(host.execute(URL, "learn")).result(20)
    assert result["status"] == "COMPLETED"
    assert not host.scopes
    # A following headless operation must not reuse the previous UI.
    result = host.submit(host.execute(URL.replace("11111111", "33333333"), "learn")).result(20)
    assert result["status"] == "COMPLETED"
    assert not host.scopes
    event("progress_finished")


async def synthetic(host, payload):
    assert asyncio.get_running_loop() is host.loop
    event("activity_entered")
    if MODE == "cancel":
        try:
            (DIRECTORY / "ready").touch()
            await asyncio.sleep(300)
        finally:
            await asyncio.sleep(0.03)
            event("cleanup")
    server = DIRECTORY / "remote.json"
    if server.exists():
        event("reconciled")
    else:
        host.repository.intent(payload["task_id"], "submit", {"record_id": "before"})
        server.write_text('{"record_id":"after"}', encoding="utf-8")
        event("external_write")
        if MODE == "crash-effect":
            os._exit(86)
    return TaskOutcome(
        payload["task_id"], Status.COMPLETED, evidence_refs=["fresh_mock_receipt"]
    )


def main():
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    host = WorkflowHost(
        DIRECTORY / "workflows",
        activities={"learn": synthetic},
        identity_provider=lambda: "test-account",
    )
    host.start()
    try:
        if MODE.startswith("progress-"):
            exercise_progress_resume(host)
            return
        if MODE == "crash-plan":
            task = host.repository.upsert(parse_entry(URL, "test-account"))
            host.repository.plan(task, "learn", {}, "course-afk-v1")
            os._exit(86)
        if MODE == "crash-outcome":
            original = host.repository.save_outcome

            def crash(*args):
                original(*args)
                os._exit(86)

            host.repository.save_outcome = crash
        if MODE == "idle":
            host.submit(asyncio.sleep(0.3)).result(3)
            event("workflow_idle")
            return
        if MODE == "wrong-account":

            async def reject():
                from core.workflow.host import VERSION

                task_id = host.repository.upsert(parse_entry(URL, "test-account"))
                run = host.repository.plan(task_id, "learn", {}, VERSION)
                payload = json.loads(run["payload"])
                host.scopes[run["run_id"]] = payload
                host.identity_provider = lambda: "different-account"
                try:
                    await host.perform(payload)
                except PermissionError:
                    event("account_rejected")
                else:
                    raise AssertionError("stale cookie scope accepted")

            host.submit(reject()).result(10)
            return
        if MODE == "old-version":
            task_id = host.repository.upsert(parse_entry(URL, "test-account"))
            run = host.repository.plan(task_id, "learn", {}, "incompatible-v0")
            result = host.submit(host.execute(URL, "learn")).result(10)
            assert result["status"] == "NEEDS_REVIEW"
            assert host.repository.task(task_id)["status"] == "NEEDS_REVIEW"
            assert host.repository.run(run["run_id"])["dispatch"] == "INCOMPATIBLE"
            event("version_preserved")
            return
        if MODE == "cancel":
            future = host.submit(host.execute(URL, "learn"))
            import time

            deadline = time.monotonic() + 10
            while not (DIRECTORY / "ready").exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("activity not started")
                time.sleep(0.02)
            # Cancel through the real host, then wait for its finally signal.
            run = host.repository.export()["runs"][0]
            host.submit(host.cancel(run["run_id"])).result(10)
            assert "cleanup" in (DIRECTORY / "events.jsonl").read_text()
            event("cancel_drained")
            try:
                future.result(5)
            except BaseException:
                pass
            return
        result = host.submit(host.execute(URL, "learn")).result(20)
        assert result["status"] == "COMPLETED", result
        event("finished")
    finally:
        host.close()


if __name__ == "__main__":
    main()
