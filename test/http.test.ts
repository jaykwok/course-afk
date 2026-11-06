import test from "node:test";
import assert from "node:assert/strict";
import type { Tab } from "nodriver-ts";
import { PlatformHttp } from "../src/browser/http.ts";
import type { Authentication, Lease } from "../src/browser/auth.ts";
import { AuthenticationError } from "../src/utils.ts";

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
