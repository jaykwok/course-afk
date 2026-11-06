import test from "node:test";
import assert from "node:assert/strict";
import { WebSocket } from "ws";
import { WebServer } from "../src/web/server.ts";
import { token } from "../src/utils.ts";
import { application } from "./helpers.ts";
async function fixture() {
  const f = await application(),
    server = new WebServer(f.app),
    url = await server.start(0),
    bootstrap = new URL(url).hash.slice("#bootstrap=".length),
    origin = server.origin;
  const exchange = await fetch(`${origin}/api/session`, {
    method: "POST",
    headers: { Origin: origin, "Content-Type": "application/json" },
    body: JSON.stringify({ bootstrap }),
  });
  const cookie = exchange.headers.get("set-cookie")!.split(";")[0],
    session = (await exchange.json()) as any;
  const headers = {
    Origin: origin,
    Cookie: cookie,
    "Content-Type": "application/json",
    "X-CSRF-Token": session.csrf,
    "X-Control-Client": session.client,
  };
  const sockets: WebSocket[] = [];
  const connect = async (client = session.client) => {
    const ws = new WebSocket(origin.replace("http:", "ws:") + "/api/events", {
      headers: { Origin: origin, Cookie: cookie },
    });
    sockets.push(ws);
    const snapshot = await new Promise<any>((resolve, reject) => {
      ws.once("error", reject);
      ws.once("open", () => ws.send(JSON.stringify({ client })));
      ws.once("message", (m) => resolve(JSON.parse(m.toString())));
    });
    return { ws, snapshot };
  };
  const post = (path: string, body: unknown, generation?: string) =>
    fetch(origin + path, {
      method: "POST",
      headers: { ...headers, "X-Connection-ID": generation || "" },
      body: JSON.stringify(body),
    });
  return {
    ...f,
    server,
    origin,
    bootstrap,
    cookie,
    session,
    headers,
    post,
    connect,
    cleanup: async () => {
      for (const ws of sockets) ws.terminate();
      await server.close();
      await f.close();
    },
  };
}
test("HTTP bootstrap single-use, origin/CSRF boundaries and observer windows", async () => {
  const f = await fixture();
  try {
    assert.equal((await fetch(f.origin + "/api/state")).status, 401);
    assert.equal((await fetch(f.origin + "/api/settings")).status, 401);
    const settings = await fetch(f.origin + "/api/settings", {
      headers: { Cookie: f.cookie },
    });
    assert.equal(settings.status, 200);
    assert.equal(((await settings.json()) as any).openai.apiKey, "");
    assert.equal(
      (await f.post("/api/session", { bootstrap: f.bootstrap })).status,
      401,
    );
    assert.equal(
      (
        await fetch(f.origin + "/api/state", {
          headers: { Cookie: f.cookie, Origin: "https://evil.test" },
        })
      ).status,
      403,
    );
    const observer = await f.connect("");
    assert.equal(observer.snapshot.controller, false);
    assert.equal(
      (
        await f.post(
          "/api/operations",
          { kind: "login", request_id: token() },
          observer.snapshot.generation,
        )
      ).status,
      409,
    );
    const writer = await f.connect();
    assert.equal(writer.snapshot.controller, true);
    assert.equal(
      (
        await fetch(f.origin + "/api/control/exit", {
          method: "POST",
          headers: {
            ...f.headers,
            "X-CSRF-Token": "bad",
            "X-Connection-ID": writer.snapshot.generation,
          },
          body: JSON.stringify({ request_id: token() }),
        })
      ).status,
      403,
    );
    f.chrome.assertDone();
  } finally {
    await f.cleanup();
  }
});
test("reconnection fences old writer and deduplicates exactly identical commands", async () => {
  const f = await fixture();
  let release!: () => void,
    calls = 0;
  f.app.execute = async () => {
    calls++;
    await new Promise<void>((r) => (release = r));
  };
  try {
    const a = await f.connect(),
      b = await f.connect();
    assert.notEqual(a.snapshot.generation, b.snapshot.generation);
    assert.equal(
      (
        await f.post(
          "/api/operations",
          { kind: "login", request_id: token() },
          a.snapshot.generation,
        )
      ).status,
      409,
    );
    const body = { kind: "login", request_id: token() };
    const first = await f.post("/api/operations", body, b.snapshot.generation),
      again = await f.post("/api/operations", body, b.snapshot.generation);
    assert.deepEqual(await first.json(), await again.json());
    assert.equal(calls, 1);
    assert.equal(
      (
        await f.post(
          "/api/operations",
          { ...body, kind: "exam" },
          b.snapshot.generation,
        )
      ).status,
      409,
    );
    release();
    await f.app.settled();
    assert.equal(f.app.operationId, null);
  } finally {
    release?.();
    await f.app.settled();
    await f.cleanup();
  }
});
test("prompt generation, cancellation and next operation release", async () => {
  const f = await fixture();
  f.app.execute = async () => {
    await f.app.prompt("confirm", { title: "Test", message: "Confirm" });
  };
  try {
    const writer = await f.connect(),
      generation = writer.snapshot.generation;
    await f.post(
      "/api/operations",
      { kind: "login", request_id: token() },
      generation,
    );
    const p = f.app.view.prompt;
    assert.equal(
      (
        await f.post(
          `/api/prompts/${p.id}/answer`,
          {
            operation_id: p.operation_id,
            version: p.version - 1,
            value: true,
            request_id: token(),
          },
          generation,
        )
      ).status,
      409,
    );
    assert.equal(
      (
        await f.post(
          "/api/control/cancel",
          { operation_id: p.operation_id, request_id: token() },
          generation,
        )
      ).status,
      200,
    );
    await f.app.settled();
    assert.equal(f.app.operationId, null);
    assert.equal(f.app.view.prompt, null);
    f.app.execute = async () => {};
    assert.equal(
      (
        await f.post(
          "/api/operations",
          { kind: "login", request_id: token() },
          generation,
        )
      ).status,
      200,
    );
    await f.app.settled();
  } finally {
    await f.cleanup();
  }
});
test("projected logs remain bounded and redact tokens", async () => {
  const f = await application();
  try {
    for (let i = 0; i < 420; i++)
      f.app.logger.log(`line ${i} https://example.test/?token=secret`);
    const s = f.app.snapshot();
    assert.equal(s.logs.length, 400);
    assert.equal(s.dropped_logs, 20);
    assert.ok(!JSON.stringify(s).includes("token=secret"));
  } finally {
    await f.close();
  }
});
