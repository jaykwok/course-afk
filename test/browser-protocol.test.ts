import test from "node:test";
import assert from "node:assert/strict";
import { writeFile, readFile } from "node:fs/promises";
import { join } from "node:path";
import {
  createProfile,
  validateProfile,
  removeProfile,
  PASSWORD_TIME,
} from "../src/browser/profile.ts";
import { application } from "./helpers.ts";
import { atomicJson } from "../src/storage/files.ts";
import { accountId } from "../src/domain/identity.ts";

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
