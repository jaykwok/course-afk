import test from "node:test";
import assert from "node:assert/strict";
import { runInNewContext } from "node:vm";
import { TimeoutError, type Tab } from "nodriver-ts";
import { waitForCasePoolSession } from "../src/browser/case-pool.ts";
import { CASE_POOL } from "../src/domain/identity.ts";
import { ReviewRequired } from "../src/utils.ts";

test("an already authenticated case pool does not trigger another login action", async () => {
  const tab = {
    closed: false,
    get frames() {
      throw new Error("must not click login");
    },
    async waitForFunction() {
      return true;
    },
  } as unknown as Tab;
  await waitForCasePoolSession(tab, new AbortController().signal, () => {});
});

test("case-pool SSO clicks once and waits for token on the returned case-pool page", async () => {
  let clicks = 0,
    attempts = 0;
  const button = {
    evaluate: async () => true,
    click: async (options: any) => {
      options.guard();
      clicks++;
    },
  };
  const tab = {
    closed: false,
    frames: [
      {
        url: CASE_POOL,
        detached: false,
        query: async () => null,
        queryAll: async () => [button],
      },
    ],
    async waitForFunction(fn: Function, target: string) {
      const url = [
        "https://center.mylearning.cn/PC/home",
        "https://www.ctexpert.cn/login",
        CASE_POOL,
      ][attempts++];
      const ready = runInNewContext(`(${fn.toString()})(target)`, {
        target,
        URL,
        location: new URL(url),
        document: { readyState: "complete" },
        localStorage: { getItem: () => "present" },
      });
      if (!ready) throw new TimeoutError("redirect pending");
    },
  } as unknown as Tab;
  await waitForCasePoolSession(
    tab,
    new AbortController().signal,
    () => {},
    1000,
  );
  assert.equal(clicks, 1);
  assert.equal(attempts, 3);
});

test("case-pool authentication can be cancelled and incomplete redirects remain retryable", async () => {
  const abort = new AbortController();
  const tab = {
    closed: false,
    frames: [],
    async waitForFunction() {
      abort.abort(new Error("cancelled"));
      throw new TimeoutError("no return");
    },
  } as unknown as Tab;
  await assert.rejects(
    waitForCasePoolSession(tab, abort.signal, () => {}),
    /cancelled/,
  );
  await assert.rejects(
    waitForCasePoolSession(tab, new AbortController().signal, () => {}, 0),
    ReviewRequired,
  );
});
