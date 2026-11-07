import test from "node:test";
import assert from "node:assert/strict";
import { runInNewContext } from "node:vm";
import type { Tab } from "nodriver-ts";
import { PlatformHttp } from "../src/browser/http.ts";
import type { Authentication, Lease } from "../src/browser/auth.ts";
import { AuthenticationError, ReviewRequired } from "../src/utils.ts";

function fixture(statuses: number[]) {
  const abort = new AbortController();
  let sends = 0,
    waits = 0,
    invalidations = 0,
    ready = false;
  const lease: Lease = {
    account: "a",
    generation: 0,
    fingerprint: "f",
    signal: abort.signal,
  };
  const auth = {
    assert() {
      abort.signal.throwIfAborted();
    },
    invalidate() {
      invalidations++;
    },
  } as unknown as Authentication;
  const tab = {
    async waitForFunction() {
      waits++;
      ready = true;
    },
    async evaluate() {
      assert.equal(ready, true, "requests must await SPA token readiness");
      sends++;
      return { status: statuses.shift(), value: { data: "ok" } };
    },
  } as unknown as Tab;
  return {
    http: new PlatformHttp(tab, auth, lease),
    tab,
    abort,
    stats: () => ({ sends, waits, invalidations }),
  };
}
test("API waits for readiness, retries reads but never retries writes", async () => {
  const read = fixture([503, 200]);
  assert.deepEqual(await read.http.json("/api/read"), { data: "ok" });
  assert.equal(read.stats().sends, 2);
  const write = fixture([503, 200]);
  await assert.rejects(
    write.http.json("/api/enroll", { method: "POST", body: {} }),
    /503/,
  );
  assert.equal(write.stats().sends, 1);
  await assert.rejects(
    write.http.json("/api/enroll", { method: "POST", readOnly: true }),
    /不可重试/,
  );
  assert.equal(write.stats().sends, 1);
});

test("API errors retain JSON business reasons and endpoint without exposing request queries", async (t) => {
  for (const [status, body, contentType, expected] of [
    [
      422,
      JSON.stringify({ errorCode: 60111, message: "Invalid input." }),
      "",
      /422.*60111.*您无权限参加该培训班/,
    ],
    [
      422,
      JSON.stringify({ errorCode: 69040, message: "培训班已关闭" }),
      "application/json",
      /422.*GET \/api\/class.*69040.*培训班已关闭/,
    ],
    [
      422,
      JSON.stringify({ code: 12, message: "参数无效" }),
      "application/problem+json",
      /422.*12.*参数无效/,
    ],
    [500, "<html>server error</html>", "text/html", /500.*GET \/api\/class/],
  ] as const) {
    const f = fixture([]);
    let requests = 0;
    t.mock.method(f.tab, "evaluate", async (fn: Function, arg: unknown) =>
      runInNewContext(`(${fn.toString()})`, {
        location: { origin: "https://kc.zhixueyun.com" },
        document: { cookie: "authorization=test-token" },
        AbortSignal,
        window: {
          require(name: string) {
            assert.equal(name, "./app/util/errors");
            return {
              erorrMap: { 60111: "permission" },
              get: () => "您无权限参加该培训班",
            };
          },
        },
        fetch: async () => {
          requests++;
          const response = new Response(body, {
            status,
            headers: { "Content-Type": contentType },
          });
          if (!contentType) response.headers.delete("Content-Type");
          return response;
        },
      })(arg),
    );
    await assert.rejects(
      f.http.json("/api/class?token=private-query"),
      (error: Error) => {
        assert.match(error.message, expected);
        assert.doesNotMatch(error.message, /private-query|test-token|<html>/);
        assert.ok(error instanceof ReviewRequired);
        assert.equal(error.recovery, status === 422 ? "blocked" : "retry");
        return true;
      },
    );
    assert.equal(requests, 1, "business errors must not be retried blindly");
  }
});
test("API authentication failure invalidates lease without retry; cancellation sends nothing", async () => {
  const denied = fixture([401, 200]);
  await assert.rejects(denied.http.json("/api/read"), AuthenticationError);
  assert.deepEqual(denied.stats(), { sends: 1, waits: 1, invalidations: 1 });
  const cancelled = fixture([200]);
  cancelled.abort.abort(new Error("cancelled"));
  await assert.rejects(cancelled.http.json("/api/read"), /cancelled/);
  assert.equal(cancelled.stats().sends, 0);
  await assert.rejects(
    fixture([200]).http.json("https://outside.test/api/read"),
    /允许范围/,
  );
});
