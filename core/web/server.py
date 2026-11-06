"""Authenticated same-origin loopback HTTP/WebSocket transport."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket

from aiohttp import web, WSMsgType

from core.web.session import ProtocolError

STATIC = Path(__file__).with_name("static")


class LocalServer:
    def __init__(self, runtime):
        self.runtime = runtime
        self.boot = secrets.token_urlsafe(32)
        self.cookie = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.client = secrets.token_urlsafe(32)
        self.reopen_key = secrets.token_hex(32)
        self.origin = None
        self.generation = None
        self.writer = None
        self.sockets = set()
        self.reopen = None
        self.runner = None
        self.instance_path = None
        self.app = web.Application(middlewares=[self.boundary], client_max_size=65536)
        self.app.add_routes([
            web.get("/", self.index), web.get("/app.js", self.asset), web.get("/style.css", self.asset),
            web.post("/api/session", self.exchange), web.post("/api/reopen", self.reopen_handler),
            web.get("/api/state", self.state), web.get("/api/events", self.events),
            web.post("/api/operations", self.command),
            web.post("/api/prompts/{prompt_id}/answer", self.command),
            web.post("/api/control/{action}", self.command),
        ])

    @web.middleware
    async def boundary(self, request, handler):
        try:
            if request.host != self.origin.removeprefix("http://"):
                raise web.HTTPForbidden(text="Host rejected")
            origin = request.headers.get("Origin")
            if origin is not None and origin != self.origin:
                raise web.HTTPForbidden(text="Origin rejected")
            if request.method == "POST":
                if request.content_type != "application/json":
                    raise web.HTTPUnsupportedMediaType()
                if request.path != "/api/reopen" and origin != self.origin:
                    raise web.HTTPForbidden(text="Origin required")
            if request.path.startswith("/api/") and request.path not in {"/api/session", "/api/reopen"}:
                if not secrets.compare_digest(request.cookies.get("course_web", ""), self.cookie):
                    raise web.HTTPUnauthorized()
                if request.method == "POST" and not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), self.csrf):
                    raise web.HTTPForbidden(text="CSRF rejected")
            response = await handler(request)
        except ProtocolError as exc:
            response = web.json_response({"error": str(exc)}, status=exc.status)
        except web.HTTPException as exc:
            if request.path.startswith("/api/"):
                response = web.json_response({"error": exc.reason}, status=exc.status)
            else:
                response = exc
        except (json.JSONDecodeError, TypeError, KeyError):
            response = web.json_response({"error": "请求格式不正确"}, status=400)
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    async def index(self, request):
        return web.FileResponse(STATIC / "index.html")

    async def asset(self, request):
        return web.FileResponse(STATIC / request.path.lstrip("/"))

    async def exchange(self, request):
        data = await self.payload(request)
        if not self.boot or not secrets.compare_digest(str(data.get("bootstrap", "")), self.boot):
            raise web.HTTPUnauthorized()
        self.boot = None
        response = web.json_response({"client": self.client, "csrf": self.csrf})
        response.set_cookie("course_web", self.cookie, httponly=True, samesite="Strict", path="/")
        return response

    async def state(self, request):
        return web.json_response(dict(**self.runtime.session.snapshot(), csrf=self.csrf))

    @staticmethod
    async def payload(request):
        data = await request.json()
        if not isinstance(data, dict):
            raise ProtocolError("请求必须是对象", 400)
        return data

    def check_writer(self, request):
        if (self.writer is None or self.writer.closed or not self.runtime.connected
            or not secrets.compare_digest(request.headers.get("X-Control-Client", ""), self.client)
            or request.headers.get("X-Connection-ID") != self.generation):
            raise ProtocolError("当前页面只读或连接已失效，请回到管理窗口", 409)

    async def command(self, request):
        self.check_writer(request)
        data = await self.payload(request)
        if request.path == "/api/operations":
            action = "operation"
        elif "prompt_id" in request.match_info:
            action = "answer"
            data["prompt_id"] = request.match_info["prompt_id"]
        else:
            action = request.match_info["action"]
        request_id = data.pop("request_id", None)
        # Reading a body yields to a replacement WebSocket. Fence again at the
        # commit point; no await may separate this check from the command.
        self.check_writer(request)
        return web.json_response(self.runtime.command(request_id, action, data))

    async def events(self, request):
        if request.headers.get("Origin") != self.origin:
            raise web.HTTPForbidden()
        if len(self.sockets) >= 8:
            raise web.HTTPTooManyRequests(text="管理页连接过多")
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=4096, compress=False)
        self.sockets.add(ws)
        generation = None
        try:
            await ws.prepare(request)
            message = await ws.receive(timeout=10)
            if message.type != WSMsgType.TEXT:
                return ws
            data = json.loads(message.data)
            if not isinstance(data, dict):
                return ws
            controller = secrets.compare_digest(str(data.get("client", "")), self.client)
            if controller:
                old = self.writer
                generation = secrets.token_hex(16)
                self.writer, self.generation = ws, generation
                self.runtime.connection(True)
                if old and old is not ws:
                    await old.close(code=4001, message=b"replaced")
            # Snapshots deliberately replace unbounded event replay: logs are a
            # finite tail, progress is coalesced, database remains authoritative.
            async with asyncio.timeout(5):
                await ws.send_json(dict(**self.runtime.session.snapshot(), controller=controller, generation=generation))
            last = -1
            while not ws.closed:
                try:
                    async with asyncio.timeout(.25):
                        message = await ws.receive()
                        if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                            break
                except TimeoutError:
                    pass
                snapshot = self.runtime.session.snapshot()
                if snapshot["seq"] != last:
                    # Slow readers can lose intermediate projections without
                    # ever blocking business producers or allocating a queue.
                    async with asyncio.timeout(5):
                        await ws.send_json(dict(**snapshot, controller=controller, generation=generation))
                    last = snapshot["seq"]
        except (TimeoutError, ConnectionError, json.JSONDecodeError, TypeError):
            pass
        finally:
            self.sockets.discard(ws)
            if generation and generation == self.generation and self.writer is ws:
                self.writer = None
                self.runtime.connection(False)
            if ws.prepared:
                await ws.close()
        return ws

    async def reopen_handler(self, request):
        data = await self.payload(request)
        if not secrets.compare_digest(str(data.get("key", "")), self.reopen_key):
            raise web.HTTPForbidden()
        challenge = str(data.get("challenge", ""))
        if len(challenge) != 32 or self.runtime.control.exit_requested or not self.reopen:
            raise ProtocolError("实例正在启动或退出")
        await self.reopen()
        proof = hmac.new(self.reopen_key.encode(), challenge.encode(), hashlib.sha256).hexdigest()
        return web.json_response({"instance_id": self.runtime.session.instance_id, "proof": proof})

    async def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            sock.listen(64)
            sock.setblocking(False)
            self.origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            self.launch_url = self.origin + "/#bootstrap=" + self.boot
            self.runner = web.AppRunner(self.app, access_log=None, shutdown_timeout=2)
            await self.runner.setup()
            await web.SockSite(self.runner, sock).start()
        except BaseException:
            sock.close()
            raise

    def write_instance(self):
        import psutil
        from core.config import DATA_DIR
        from core.storage import write_json_atomic
        self.instance_path = DATA_DIR / "web-instance.json"
        write_json_atomic(self.instance_path, dict(pid=os.getpid(), created=psutil.Process().create_time(),
            origin=self.origin, instance_id=self.runtime.session.instance_id, key=self.reopen_key))
        if os.name != "nt":
            self.instance_path.chmod(0o600)

    async def close(self):
        if self.sockets:
            await asyncio.gather(*(ws.close(code=1001) for ws in tuple(self.sockets)), return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
        if self.instance_path:
            self.instance_path.unlink(missing_ok=True)


def reopen_existing(path):
    """Verify process creation time and a live instance challenge before focusing."""
    import httpx
    import psutil
    from urllib.parse import urlsplit
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        parsed = urlsplit(data["origin"])
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.path or parsed.username or parsed.password or parsed.query or parsed.fragment):
            return False
        if psutil.Process(data["pid"]).create_time() != data["created"]:
            return False
        challenge = secrets.token_hex(16)
        response = httpx.post(data["origin"] + "/api/reopen", json=dict(key=data["key"], challenge=challenge),
                              timeout=3, trust_env=False, follow_redirects=False)
        result = response.json()
        proof = hmac.new(data["key"].encode(), challenge.encode(), hashlib.sha256).hexdigest()
        return (response.status_code == 200 and result.get("instance_id") == data["instance_id"]
                and hmac.compare_digest(str(result.get("proof", "")), proof))
    except (OSError, ValueError, KeyError, TypeError, psutil.Error, httpx.HTTPError):
        return False
