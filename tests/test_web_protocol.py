import asyncio
import json
import secrets
import threading
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, CookieJar

from core.abort import UserCancelRequested
from core.app.control_session import ControlSession
from core.app.runtime import ApplicationRuntime
from core.web.server import LocalServer
from core.web.session import ProtocolError, WebSession


def rid():
    return secrets.token_hex(16)


class WebProjectionTests(unittest.TestCase):
    def test_logs_are_bounded_redacted_and_progress_is_coalesced(self):
        s = WebSession(ControlSession())
        for i in range(1500):
            s.log(f"{i} token=private https://local.test/#bootstrap=hidden")
            s.update(progress={"completed": i})
        snapshot = s.snapshot()
        self.assertEqual(len(snapshot["logs"]), 400)
        self.assertEqual(snapshot["dropped_logs"], 1100)
        self.assertEqual(snapshot["progress"]["completed"], 1499)
        self.assertNotIn("private", json.dumps(snapshot))
        self.assertNotIn("hidden", json.dumps(snapshot))

    def test_prompt_answer_fences_and_cancel_wins(self):
        c = ControlSession(); c.begin("one", "exam")
        s = WebSession(c)
        p = s.prepare_prompt("confirm", message="自动交卷？", default=False)
        for operation, version, value in [("old", p["version"], True), ("one", 0, True), ("one", p["version"], 1)]:
            with self.assertRaises(ProtocolError):
                s.answer(p["id"], operation, version, value)
        s.answer(p["id"], "one", p["version"], False)
        with self.assertRaises(ProtocolError):
            s.answer(p["id"], "one", p["version"], True)
        c.cancel()
        with self.assertRaises(UserCancelRequested):
            s.wait_prompt(p)
        self.assertIsNone(s.snapshot()["prompt"])


class WebServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        def dispatcher(kind, ui):
            self.calls.append(kind)
            ui.prompt_yes_no("默认不自动交卷", default="N")
        self.runtime = ApplicationRuntime(dispatcher=dispatcher)
        self.runtime.start()
        self.server = LocalServer(self.runtime)
        await self.server.start()
        self.http = ClientSession(cookie_jar=CookieJar(unsafe=True))
        self.origin = self.server.origin
        self.ws = None

    async def asyncTearDown(self):
        if self.ws:
            await self.ws.close()
        await self.http.close()
        await self.runtime.close()
        await self.server.close()

    async def authenticate(self):
        response = await self.http.post(self.origin + "/api/session", headers={"Origin": self.origin},
                                        json={"bootstrap": self.server.boot})
        self.assertEqual(response.status, 200)
        self.auth = await response.json()
        self.ws = await self.http.ws_connect(self.origin + "/api/events", origin=self.origin)
        await self.ws.send_json({"client": self.auth["client"]})
        snapshot = await self.ws.receive_json()
        self.headers = {"Origin": self.origin, "X-CSRF-Token": self.auth["csrf"],
                        "X-Control-Client": self.auth["client"], "X-Connection-ID": snapshot["generation"]}
        return snapshot

    async def post(self, path, payload, *, headers=None):
        response = await self.http.post(self.origin + path, json=payload, headers=headers or self.headers)
        return response.status, await response.json()

    async def prompt(self):
        async with asyncio.timeout(4):
            while True:
                snapshot = self.runtime.session.snapshot()
                if snapshot["prompt"]:
                    return snapshot["prompt"]
                await asyncio.sleep(.01)

    async def test_loopback_auth_origin_host_csrf_and_one_time_exchange(self):
        response = await self.http.get(self.origin + "/api/state")
        self.assertEqual(response.status, 401)
        await self.authenticate()
        response = await self.http.get(self.origin + "/api/events", headers={"Origin": self.origin})
        self.assertEqual(response.status, 400, "a malformed WebSocket handshake must be rejected cleanly")
        for headers in [{"Origin":"https://evil.invalid"}, {"Host":"evil.invalid"}, {"Origin": self.origin}]:
            response = await self.http.post(self.origin+"/api/control/exit", json={}, headers=headers)
            self.assertEqual(response.status,403)
        response = await self.http.post(self.origin+"/api/session", json={"bootstrap":"old"}, headers={"Origin":self.origin})
        self.assertEqual(response.status,401)
        response = await self.http.get(self.origin+"/")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    async def test_duplicate_start_busy_stale_and_duplicate_answers(self):
        await self.authenticate()
        payload = {"request_id": rid(), "kind":"exam"}
        code, first = await self.post("/api/operations", payload)
        self.assertEqual(code,200)
        self.assertEqual(await self.post("/api/operations", payload), (200,first))
        self.assertEqual((await self.post("/api/operations", {"request_id":rid(),"kind":"learn"}))[0],409)
        p = await self.prompt()
        self.assertFalse(p["default"])
        answer = {"request_id":rid(),"operation_id":first["operation_id"],"version":p["version"],"value":False}
        path = f"/api/prompts/{p['id']}/answer"
        self.assertEqual((await self.post(path,answer))[0],200)
        self.assertEqual((await self.post(path,answer))[0],200)
        self.assertEqual((await self.post(path,{**answer,"request_id":rid(),"value":True}))[0],409)
        self.assertEqual(self.calls,["exam"])

    async def test_refresh_reconnect_replays_prompt_and_old_disconnect_cannot_stop_new_writer(self):
        await self.authenticate()
        await self.post("/api/operations", {"request_id":rid(),"kind":"links"})
        prompt = await self.prompt()
        old_ws, old_headers = self.ws, self.headers.copy()
        newer = await self.http.ws_connect(self.origin+"/api/events", origin=self.origin)
        await newer.send_json({"client": self.auth["client"]})
        snapshot = await newer.receive_json()
        self.assertEqual(snapshot["prompt"], prompt)
        self.headers["X-Connection-ID"] = snapshot["generation"]
        await old_ws.close()
        self.ws = newer
        self.assertTrue(self.runtime.connected)
        code, _ = await self.post("/api/control/cancel", {"request_id":rid(),"operation_id":prompt["operation_id"]}, headers=old_headers)
        self.assertEqual(code,409)
        reader = await self.http.ws_connect(self.origin+"/api/events", origin=self.origin)
        await reader.send_json({"client":""})
        self.assertFalse((await reader.receive_json())["controller"])
        await reader.close()
        self.assertTrue(self.runtime.connected)
        self.assertEqual((await self.post("/api/control/cancel", {"request_id":rid(),"operation_id":prompt["operation_id"]}))[0],200)
        async with asyncio.timeout(3):
            while self.runtime.control.operation_id:
                await asyncio.sleep(.01)
        self.assertIsNone(self.runtime.session.snapshot()["prompt"])

    async def replace_writer_while_post_waits_for_body(self, path, payload):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.server.payload

        async def delayed_payload(request):
            entered.set()
            await release.wait()
            return await original(request)

        with patch.object(self.server, "payload", side_effect=delayed_payload):
            old_headers = self.headers.copy()
            pending = asyncio.create_task(self.post(path, payload, headers=old_headers))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                old = self.ws
                self.ws = await self.http.ws_connect(self.origin + "/api/events", origin=self.origin)
                await self.ws.send_json({"client": self.auth["client"]})
                snapshot = await self.ws.receive_json()
                self.assertNotEqual(snapshot["generation"], old_headers["X-Connection-ID"])
                self.headers["X-Connection-ID"] = snapshot["generation"]
                await old.close()
            finally:
                release.set()
            code, result = await asyncio.wait_for(pending, 3)
        self.assertEqual(code, 409, result)
        self.assertNotIn(payload["request_id"], self.runtime.commands)

    async def test_inflight_start_from_replaced_writer_never_dispatches(self):
        await self.authenticate()
        await self.replace_writer_while_post_waits_for_body(
            "/api/operations", {"request_id": rid(), "kind": "exam"})
        self.assertIsNone(self.runtime.control.operation_id)
        self.assertEqual(self.calls, [])

    async def test_inflight_answer_from_replaced_writer_cannot_resolve_prompt(self):
        await self.authenticate()
        await self.post("/api/operations", {"request_id": rid(), "kind": "exam"})
        prompt = await self.prompt()
        await self.replace_writer_while_post_waits_for_body(
            f"/api/prompts/{prompt['id']}/answer",
            {"request_id": rid(), "operation_id": prompt["operation_id"],
             "version": prompt["version"], "value": True})
        self.assertEqual(self.runtime.session.snapshot()["prompt"], prompt)
        self.assertIsNone(self.runtime.session._answered_prompt)

    async def test_inflight_controls_from_replaced_writer_do_not_change_operation(self):
        await self.authenticate()
        await self.post("/api/operations", {"request_id": rid(), "kind": "learn"})
        prompt = await self.prompt()
        for action in ("cancel", "stop-after-current", "exit"):
            with self.subTest(action=action):
                await self.replace_writer_while_post_waits_for_body(
                    "/api/control/" + action,
                    {"request_id": rid(), "operation_id": prompt["operation_id"]})
                control = self.runtime.control.snapshot()
                self.assertEqual(control["operation_id"], prompt["operation_id"])
                self.assertFalse(control["cancelled"])
                self.assertFalse(control["stop_after_current"])
                self.assertFalse(control["exit_requested"])


class ApplicationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_borrowed_workflow_loop_rejects_sync_start_and_close_on_itself(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from core.workflow.host import WorkflowHost
        with TemporaryDirectory() as tmp:
            host = WorkflowHost(Path(tmp), loop=asyncio.get_running_loop())
            with self.assertRaisesRegex(RuntimeError, '自己的事件循环'):
                host.start()
            with self.assertRaisesRegex(RuntimeError, '自己的事件循环'):
                host.close()

    async def test_cancel_between_controller_check_and_async_registration_cannot_start_work(self):
        from types import SimpleNamespace
        from core.runtime import run_operation
        entered=threading.Event();release=threading.Event();ran=[]
        loop=asyncio.get_running_loop()
        host=SimpleNamespace(loop=loop,submit=lambda coro:asyncio.run_coroutine_threadsafe(coro,loop))
        def get_host():
            entered.set();release.wait(4)
            return host
        async def work():ran.append("started")
        runtime=ApplicationRuntime(dispatcher=lambda kind,ui:run_operation(work()))
        with patch("core.workflow.host.get_host",side_effect=get_host):
            runtime.start();runtime.command(rid(),"operation",{"kind":"learn"})
            async with asyncio.timeout(3):
                while not entered.is_set():await asyncio.sleep(.01)
            runtime.cancel();release.set()
            async with asyncio.timeout(3):
                while runtime.control.operation_id:await asyncio.sleep(.01)
            await runtime.close()
        self.assertEqual(ran,[])

    async def test_server_start_failure_stops_worker_and_restores_handlers(self):
        import logging
        import signal
        from core.app.runtime import serve_application
        runtime=ApplicationRuntime()
        previous=list(logging.getLogger().handlers)
        previous_term=signal.getsignal(signal.SIGTERM)
        with (patch("core.app.runtime.ApplicationRuntime",return_value=runtime),
              patch("core.web.server.LocalServer.start",new=AsyncMock(side_effect=RuntimeError("startup failure"))),
              patch("core.web.server.LocalServer.close",new=AsyncMock(side_effect=RuntimeError("cleanup failure")))):
            with self.assertRaisesRegex(RuntimeError,"startup failure"):
                await serve_application()
        self.assertFalse(runtime.worker.is_alive())
        self.assertEqual(logging.getLogger().handlers,previous)
        self.assertEqual(signal.getsignal(signal.SIGTERM),previous_term)

    async def test_pre_dispatch_cancel_unblocks_worker_and_new_batch_resets_stop(self):
        runtime=ApplicationRuntime(dispatcher=lambda kind, ui: ui.pause())
        runtime.command(rid(),"operation",{"kind":"learn"})
        runtime.cancel()  # queued but worker not started yet
        runtime.start()
        async with asyncio.timeout(3):
            while runtime.control.operation_id: await asyncio.sleep(.01)
        self.assertFalse(runtime.control.cancelled)
        runtime.control.stop()
        runtime.command(rid(),"operation",{"kind":"tasks"})
        self.assertFalse(runtime.control.stop_after_current)
        await runtime.close()
        self.assertFalse(runtime.worker.is_alive())

    async def test_close_during_human_prompt_cancels_without_default_answer(self):
        accepted=[]
        def dispatcher(kind,ui):
            accepted.append(ui.prompt_yes_no("交卷？",default="N"))
        runtime=ApplicationRuntime(dispatcher=dispatcher);runtime.start()
        runtime.command(rid(),"operation",{"kind":"learn"})
        async with asyncio.timeout(3):
            while not runtime.session.snapshot()["prompt"]: await asyncio.sleep(.01)
        runtime.request_exit(graceful=True)
        await runtime.close()
        self.assertEqual(accepted,[])

    def test_disconnect_grace_suspend_and_close_semantics(self):
        now=[0.0]
        runtime=ApplicationRuntime(clock=lambda:now[0])
        runtime.control.begin("one","learn")
        for second in range(1,61):
            now[0]=second;runtime.tick()
        self.assertFalse(runtime.control.exit_requested)
        now[0]=61;runtime.tick()
        self.assertTrue(runtime.control.exit_requested)
        self.assertFalse(runtime.control.cancelled)
        runtime.frontend.close()

    def test_learning_close_deadline_enters_bounded_cancellation(self):
        now=[10.0];runtime=ApplicationRuntime(clock=lambda:now[0])
        runtime.control.begin("learning","learn")
        runtime.request_exit(graceful=True)
        self.assertFalse(runtime.tick())
        now[0]=1811
        self.assertTrue(runtime.tick())
        self.assertTrue(runtime.control.cancelled)
        runtime.frontend.close()

    def test_resume_from_sleep_gets_new_connection_grace(self):
        now=[0.0]
        now[0]=0
        runtime=ApplicationRuntime(clock=lambda:now[0])
        runtime.control.begin("two","exam")
        now[0]=600;runtime.tick() # delayed tick after system sleep
        self.assertFalse(runtime.control.exit_requested)
        for second in range(601,662):now[0]=second;runtime.tick()
        self.assertTrue(runtime.control.cancelled)
        runtime.frontend.close()

    async def test_stop_at_boundary_prevents_next_activity_and_preserves_plans(self):
        from tempfile import TemporaryDirectory
        from core.app.service import ApplicationService
        from core.workflow.contracts import TaskOutcome, Status
        from tests.workflow_harness import ActivityHost
        c=ControlSession();c.begin("batch","learn")
        async def first(host,payload):
            c.stop()
            return TaskOutcome(payload["task_id"],Status.COMPLETED)
        with TemporaryDirectory() as tmp:
            host=ActivityHost(tmp,{"learn":first})
            results=await ApplicationService(host,control=c).batch(["https://example.invalid/one","https://example.invalid/two"],"learn")
            self.assertEqual(len(results),1)
            self.assertEqual(len(host.repository.export()["runs"]),2)
