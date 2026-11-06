import {
  createServer,
  type IncomingMessage,
  type ServerResponse,
} from "node:http";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { timingSafeEqual } from "node:crypto";
import { WebSocketServer, WebSocket } from "ws";
import { token } from "../utils.ts";
import { Application, ProtocolError } from "../application/runtime.ts";
import { publicSettings } from "../config.ts";

const equal = (a: unknown, b: string) =>
  typeof a === "string" &&
  Buffer.byteLength(a) === Buffer.byteLength(b) &&
  timingSafeEqual(Buffer.from(a), Buffer.from(b));
export class WebServer {
  readonly app: Application;
  readonly server;
  readonly sockets = new WebSocketServer({
    noServer: true,
    maxPayload: 4096,
    perMessageDeflate: false,
  });
  origin = "";
  private bootstrap = token();
  private cookie = token();
  private csrf = token();
  private client = token();
  readonly reopenKey = token();
  private writer?: WebSocket;
  private generation = "";
  private timer?: NodeJS.Timeout;
  reopen?: () => Promise<void>;
  constructor(app: Application) {
    this.app = app;
    this.server = createServer((req, res) => {
      void this.handle(req, res).catch((e) => {
        app.logger.log(e, "error");
        if (!res.headersSent) this.json(res, 500, { error: "服务请求失败" });
        else res.end();
      });
    });
    this.server.requestTimeout = 15000;
    this.server.headersTimeout = 10000;
    this.server.on("upgrade", (req, socket, head) => {
      try {
        this.boundary(req);
        if (
          req.url !== "/api/events" ||
          req.headers.origin !== this.origin ||
          this.sockets.clients.size >= 8
        )
          throw new ProtocolError("连接不可用", 403);
        this.sockets.handleUpgrade(req, socket, head, (ws) => this.events(ws));
      } catch {
        socket.end("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
      }
    });
  }
  async start() {
    await new Promise<void>((resolve, reject) => {
      this.server.once("error", reject);
      this.server.listen(0, "127.0.0.1", () => resolve());
    });
    const address = this.server.address();
    if (!address || typeof address === "string")
      throw new Error("服务监听失败");
    this.origin = `http://127.0.0.1:${address.port}`;
    this.timer = setInterval(() => {
      for (const ws of this.sockets.clients) {
        if (ws.readyState !== WebSocket.OPEN) continue;
        if (ws.bufferedAmount > 256 * 1024) {
          ws.terminate();
          continue;
        }
        this.send(ws);
      }
    }, 500);
    this.timer.unref();
    return `${this.origin}/#bootstrap=${this.bootstrap}`;
  }
  private boundary(req: IncomingMessage) {
    if (req.headers.host !== this.origin.slice(7))
      throw new ProtocolError("Host rejected", 403);
    if (req.headers.origin !== undefined && req.headers.origin !== this.origin)
      throw new ProtocolError("Origin rejected", 403);
    const path = req.url || "/";
    if (
      req.method === "POST" &&
      (!req.headers["content-type"]?.startsWith("application/json") ||
        (path !== "/api/reopen" && req.headers.origin !== this.origin))
    )
      throw new ProtocolError("请求来源或格式不正确", 403);
    if (
      path.startsWith("/api/") &&
      !["/api/session", "/api/reopen"].includes(path)
    ) {
      const c = (req.headers.cookie || "")
        .split(";")
        .map((v) => v.trim())
        .find((v) => v.startsWith("course_web="))
        ?.slice(11);
      if (!equal(c, this.cookie))
        throw new ProtocolError("请从启动入口打开工作台", 401);
      if (
        req.method === "POST" &&
        !equal(req.headers["x-csrf-token"], this.csrf)
      )
        throw new ProtocolError("请求令牌无效", 403);
    }
  }
  private writerCheck(req: IncomingMessage) {
    if (
      !this.writer ||
      this.writer.readyState !== WebSocket.OPEN ||
      !this.app.connected ||
      !equal(req.headers["x-control-client"], this.client) ||
      !equal(req.headers["x-connection-id"], this.generation)
    )
      throw new ProtocolError("当前窗口只读或连接已失效");
  }
  private async payload(req: IncomingMessage) {
    const chunks: Buffer[] = [];
    let size = 0;
    for await (const chunk of req) {
      size += chunk.length;
      if (size > 65536) throw new ProtocolError("请求内容过长", 413);
      chunks.push(Buffer.from(chunk));
    }
    let value;
    try {
      value = JSON.parse(Buffer.concat(chunks).toString());
    } catch {
      throw new ProtocolError("请求格式不正确", 400);
    }
    if (!value || typeof value !== "object" || Array.isArray(value))
      throw new ProtocolError("请求必须为对象", 400);
    return value;
  }
  private headers(res: ServerResponse) {
    res.setHeader("Cache-Control", "no-store");
    res.setHeader("Referrer-Policy", "no-referrer");
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("X-Frame-Options", "DENY");
    res.setHeader(
      "Content-Security-Policy",
      "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    );
  }
  private json(res: ServerResponse, status: number, body: unknown) {
    this.headers(res);
    res.writeHead(status, {
      "Content-Type": "application/json; charset=utf-8",
    });
    res.end(JSON.stringify(body));
  }
  private async handle(req: IncomingMessage, res: ServerResponse) {
    try {
      this.boundary(req);
      const path = req.url || "/";
      if (
        req.method === "GET" &&
        ["/", "/app.js", "/style.css", "/logo.svg"].includes(path)
      ) {
        const name = path === "/" ? "index.html" : path.slice(1),
          file =
            name === "app.js"
              ? join(this.app.config.root, "dist/src/web/client.js")
              : join(this.app.config.root, "dist/src/web/static", name);
        const body = await readFile(file);
        this.headers(res);
        res.writeHead(200, {
          "Content-Type": name.endsWith(".html")
            ? "text/html; charset=utf-8"
            : name.endsWith(".js")
              ? "text/javascript; charset=utf-8"
              : name.endsWith(".css")
                ? "text/css; charset=utf-8"
                : "image/svg+xml",
        });
        res.end(body);
        return;
      }
      if (req.method === "GET" && path === "/api/state") {
        this.json(res, 200, { ...this.app.snapshot(), csrf: this.csrf });
        return;
      }
      if (req.method === "GET" && path === "/api/settings") {
        this.json(res, 200, publicSettings(this.app.config.settingsPath));
        return;
      }
      if (req.method !== "POST") throw new ProtocolError("路径不存在", 404);
      if (path === "/api/session") {
        const p = await this.payload(req);
        if (!this.bootstrap || !equal(p.bootstrap, this.bootstrap))
          throw new ProtocolError("启动凭据无效或已使用", 401);
        this.bootstrap = "";
        res.setHeader(
          "Set-Cookie",
          `course_web=${this.cookie}; HttpOnly; SameSite=Strict; Path=/`,
        );
        this.json(res, 200, { csrf: this.csrf, client: this.client });
        return;
      }
      if (path === "/api/reopen") {
        const p = await this.payload(req);
        if (!equal(p.key, this.reopenKey))
          throw new ProtocolError("重开令牌无效", 403);
        await this.reopen?.();
        this.json(res, 200, { ok: true });
        return;
      }
      this.writerCheck(req);
      const p = await this.payload(req);
      this.writerCheck(req);
      let action = "";
      if (path === "/api/operations") action = "operation";
      else if (/^\/api\/control\/[\w-]+$/.test(path))
        action = path.split("/").at(-1)!;
      else {
        const match = path.match(/^\/api\/prompts\/([\w-]+)\/answer$/);
        if (match) {
          action = "answer";
          p.prompt_id = match[1];
        }
      }
      if (!action) throw new ProtocolError("路径不存在", 404);
      const id = p.request_id;
      delete p.request_id;
      this.json(res, 200, this.app.command(id, action, p));
    } catch (e) {
      this.json(res, e instanceof ProtocolError ? e.status : 500, {
        error:
          e instanceof ProtocolError ? e.message : "请求执行失败，请查看日志",
      });
      if (!(e instanceof ProtocolError)) this.app.logger.log(e, "error");
    }
  }
  private events(ws: WebSocket) {
    let ready = false;
    const timeout = setTimeout(() => {
      if (!ready) ws.terminate();
    }, 10000);
    timeout.unref();
    ws.once("message", (raw) => {
      try {
        const value = JSON.parse(raw.toString());
        if (!value || typeof value !== "object") throw new Error();
        ready = true;
        clearTimeout(timeout);
        if (equal(value.client, this.client)) {
          const old = this.writer;
          this.writer = ws;
          this.generation = token();
          this.app.connection(true);
          old?.close(4001, "replaced");
        }
        this.send(ws);
      } catch {
        ws.close(1008, "invalid");
      }
    });
    ws.on("error", () => {});
    ws.on("close", () => {
      clearTimeout(timeout);
      if (this.writer === ws) {
        this.writer = undefined;
        this.generation = "";
        this.app.connection(false);
      }
    });
    (ws as WebSocket & { ready?: () => boolean }).ready = () => ready;
  }
  private send(ws: WebSocket) {
    if (!(ws as WebSocket & { ready?: () => boolean }).ready?.()) return;
    ws.send(
      JSON.stringify({
        ...this.app.snapshot(),
        controller: ws === this.writer,
        generation: ws === this.writer ? this.generation : null,
      }),
    );
  }
  async close() {
    if (this.timer) clearInterval(this.timer);
    for (const ws of this.sockets.clients) ws.terminate();
    this.sockets.close();
    this.server.closeIdleConnections();
    await new Promise<void>((resolve, reject) =>
      this.server.close((e) => (e ? reject(e) : resolve())),
    );
  }
}
