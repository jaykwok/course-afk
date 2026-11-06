import test from "node:test";
import assert from "node:assert/strict";
import { writeFile, readFile } from "node:fs/promises";
import { join } from "node:path";
import {
  createProfile,
  validateProfile,
  removeProfile,
  PASSWORD_TIME,
  prepareProjectProfile,
} from "../src/browser/profile.ts";
import { application, temporary } from "./helpers.ts";
import { atomicJson } from "../src/storage/files.ts";
import { accountId } from "../src/domain/identity.ts";
import { Browser, ProtocolError } from "nodriver-ts";
import { BrowserRuntime } from "../src/browser/runtime.ts";
import { Logger } from "../src/log.ts";
import type { Protocol } from "devtools-protocol";
import { NO_REPLY } from "nodriver-ts/testing";
import { navigateDocument } from "../src/browser/navigation.ts";

test("project profile preserves existing state, reapplies local-only settings and refuses personal profiles", async () => {
  const temp = await temporary();
  try {
    const dir = join(temp.dir, "browser-profile");
    await prepareProjectProfile(dir);
    const prefsPath = join(dir, "Default", "Preferences");
    const prefs = JSON.parse(await readFile(prefsPath, "utf8"));
    prefs.custom = { firstRunDismissed: true };
    prefs.signin.allowed = true;
    await atomicJson(prefsPath, prefs);
    await writeFile(join(dir, "Default", "Cookies"), "synthetic cookie store");
    await prepareProjectProfile(dir);
    await validateProfile(dir);
    const restored = JSON.parse(await readFile(prefsPath, "utf8"));
    assert.deepEqual(restored.custom, { firstRunDismissed: true });
    assert.equal(restored.signin.allowed, false);
    assert.equal(
      await readFile(join(dir, "Default", "Cookies"), "utf8"),
      "synthetic cookie store",
    );
    await atomicJson(join(temp.dir, "personal", "Local State"), {
      personal: true,
    });
    await assert.rejects(
      prepareProjectProfile(join(temp.dir, "personal")),
      /不属于此项目/,
    );
    assert.deepEqual(
      JSON.parse(
        await readFile(join(temp.dir, "personal", "Local State"), "utf8"),
      ),
      { personal: true },
    );
  } finally {
    await temp.close();
  }
});

test("persistent browser launch stays visible, disables extensions and retains its profile after close", async (t) => {
  const f = await application();
  try {
    const dir = join(f.temp.dir, "browser-profile");
    t.mock.method(Browser, "launch", async (options: any) => {
      assert.equal(options.headless, false);
      assert.equal(options.userDataDir, dir);
      assert.ok(options.args.includes("--disable-extensions"));
      assert.ok(options.args.includes("--disable-sync"));
      assert.ok(!options.args.includes("--no-sandbox"));
      return f.browser;
    });
    const runtime = await BrowserRuntime.launch(
      new Logger(join(f.temp.dir, "logs")),
      process.execPath,
      dir,
    );
    await runtime.close();
    await validateProfile(dir);
    assert.ok(await readFile(join(dir, "course-afk-profile.json"), "utf8"));
  } finally {
    await f.close();
  }
});

test("navigation follows rapid SSO replacements and retries only destroyed document contexts", async () => {
  const f = await application();
  try {
    const tab = await f.runtime.context.newTab();
    const frame = (loaderId: string, url: string) =>
      ({
        id: tab.mainFrame.id,
        loaderId,
        url,
        domainAndRegistry: "",
        securityOrigin: new URL(url).origin,
        mimeType: "text/html",
        secureContextType: "Secure",
        crossOriginIsolatedContextType: "NotIsolated",
        gatedAPIFeatures: [],
      }) as Protocol.Page.Frame;
    f.chrome.handle("Page.navigate", ({ url }, command) => {
      // Both commits arrive before the command reply; only the replacement
      // document emits DOMContentLoaded, as with an immediate SSO form submit.
      for (const [loader, target] of [
        ["sso", url],
        ["final", "https://center.mylearning.cn/PC/home"],
      ])
        f.chrome.emit(
          "Page.frameNavigated",
          { frame: frame(loader, target), type: "Navigation" },
          command.sessionId,
        );
      return { frameId: tab.mainFrame.id, loaderId: "sso" };
    });
    f.chrome.handle("Page.createIsolatedWorld", () => ({
      executionContextId: 7,
    }));
    let evaluations = 0;
    f.chrome.handle("Runtime.callFunctionOn", () => {
      if (++evaluations === 1)
        throw new ProtocolError(
          "Runtime.callFunctionOn",
          -32000,
          "Execution context was destroyed",
        );
      return { result: { type: "boolean", value: true } };
    });
    await navigateDocument(
      tab,
      "https://www.mylearning.cn/login",
      new AbortController().signal,
      1500,
    );
    assert.equal(tab.url, "https://center.mylearning.cn/PC/home");
    assert.equal(evaluations, 2);
    f.chrome.handle("Runtime.callFunctionOn", () => {
      throw new ProtocolError("Runtime.callFunctionOn", -32601, "unsupported");
    });
    await assert.rejects(
      navigateDocument(
        tab,
        "https://www.mylearning.cn/login",
        new AbortController().signal,
        1500,
      ),
      /unsupported/,
    );
    f.chrome.assertDone();
  } finally {
    await f.close();
  }
});

test("navigation timeouts, cancellation and failed requests close owned tabs without touching management", async () => {
  const f = await application();
  try {
    const admin = (f.runtime.management = await f.runtime.context.newTab());
    const abort = new AbortController(),
      scope = f.runtime.scope(abort.signal);
    f.chrome.handle("Page.navigate", () => NO_REPLY);
    await assert.rejects(
      scope.open("https://www.mylearning.cn/", 100),
      /timed out|timeout/i,
    );
    assert.equal(scope.tabs.size, 0);
    f.chrome.handle("Page.navigate", () => {
      abort.abort(new Error("cancelled"));
      return NO_REPLY;
    });
    await assert.rejects(scope.open("https://www.mylearning.cn/"), /cancelled/);
    assert.equal(scope.tabs.size, 0);
    const next = f.runtime.scope(new AbortController().signal);
    f.chrome.handle("Page.navigate", () => ({
      frameId: "unused",
      errorText: "net::ERR_NAME_NOT_RESOLVED",
    }));
    await assert.rejects(
      next.open("https://www.mylearning.cn/"),
      /ERR_NAME_NOT_RESOLVED/,
    );
    assert.equal(next.tabs.size, 0);
    assert.equal(admin.closed, false);
    await next.close();
    f.chrome.assertDone();
  } finally {
    await f.close();
  }
});

test("profile seeds password probe result and timestamp, fails closed on corruption", async () => {
  const dir = await createProfile();
  try {
    await validateProfile(dir);
    const local = JSON.parse(await readFile(join(dir, "Local State"), "utf8"));
    assert.equal(local.password_manager.os_password_blank, false);
    assert.equal(
      local.password_manager.os_password_last_changed,
      PASSWORD_TIME,
    );
    delete local.password_manager.os_password_last_changed;
    await writeFile(join(dir, "Local State"), JSON.stringify(local));
    await assert.rejects(validateProfile(dir), /阻止启动/);
    await assert.rejects(removeProfile(process.cwd()), /拒绝清理/);
  } finally {
    await removeProfile(dir);
  }
});
test("protocol tabs share one context; scope cleanup preserves management and review tabs", async () => {
  const f = await application();
  try {
    f.runtime.management = await f.runtime.context.newTab();
    const scope = f.runtime.scope(new AbortController().signal);
    const work = await scope.open(),
      review = await scope.open();
    scope.retain(review);
    assert.equal(work.context, f.runtime.management.context);
    assert.equal(work.inputBehavior.policy.mode, "human");
    await scope.close();
    assert.equal(work.closed, true);
    assert.equal(review.closed, false);
    assert.equal(f.runtime.management.closed, false);
    assert.equal(
      f.chrome.commands.some((c) =>
        [
          "Runtime.enable",
          "Runtime.addBinding",
          "Console.enable",
          "Debugger.enable",
        ].includes(c.method),
      ),
      false,
    );
    f.chrome.assertDone();
  } finally {
    await f.close();
  }
});
test("abort closes only the owned working tabs", async () => {
  const f = await application();
  try {
    f.runtime.management = await f.runtime.context.newTab();
    const abort = new AbortController(),
      scope = f.runtime.scope(abort.signal),
      work = await scope.open();
    abort.abort();
    await scope.close();
    assert.equal(work.closed, true);
    assert.equal(f.runtime.management.closed, false);
    await assert.rejects(scope.open());
    f.chrome.assertDone();
  } finally {
    await f.close();
  }
});
test("session clearing targets a page CDP session and preserves the management page", async () => {
  const f = await application();
  try {
    const admin = await f.runtime.context.newTab();
    f.runtime.management = admin;
    f.chrome.handle("Storage.getCookies", () => ({ cookies: [] }));
    const origins: string[] = [];
    f.chrome.handle("Storage.clearDataForOrigin", (params, command) => {
      assert.equal(command.sessionId, admin.cdp.id);
      assert.equal(params.origin.startsWith("http://127.0.0.1"), false);
      origins.push(params.origin);
    });
    f.runtime.note("https://sso.example.test/login");
    await f.runtime.clearPlatform();
    assert.equal(admin.closed, false);
    assert.ok(origins.includes("https://sso.example.test"));
    assert.ok(origins.includes("https://www.ctexpert.cn"));
    assert.ok(origins.includes("https://ctexpert.cn"));
    f.chrome.assertDone();
  } finally {
    await f.close();
  }
});
test("credential content changes invalidate the lease even when metadata is unchanged", async () => {
  const f = await application();
  try {
    const c = {
      version: 2,
      account: "center:test",
      name: "Test",
      expires: new Date(Date.now() + 86400000).toISOString(),
      cookies: [
        { name: "x", value: "one", domain: ".mylearning.cn", path: "/" },
      ],
    };
    await atomicJson(f.app.auth.path, c);
    const auth = f.app.auth as any;
    auth.current = c;
    auth.verified = true;
    auth.fingerprint = f.app.auth.fileFingerprint();
    const lease = {
      account: accountId(c.account),
      generation: 0,
      fingerprint: auth.fingerprint,
      signal: new AbortController().signal,
    };
    f.app.auth.assert(lease);
    await atomicJson(f.app.auth.path, {
      ...c,
      cookies: [{ ...c.cookies[0], value: "two" }],
    });
    assert.throws(() => f.app.auth.assert(lease), /凭证或账号/);
  } finally {
    await f.close();
  }
});
