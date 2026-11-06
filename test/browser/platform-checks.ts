import assert from "node:assert/strict";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import type { BrowserRuntime } from "../../src/browser/runtime.ts";
import { inspectAccess, clickPlayerControl } from "../../src/platform/dom.ts";
import { AuthenticationError, ReviewRequired } from "../../src/utils.ts";

/** Reuses the caller's Browser; all pages here are synthetic and local. */
export async function platformChecks(runtime: BrowserRuntime) {
  const server = createServer((request, response) => {
    response.setHeader("Content-Type", "text/html; charset=utf-8");
    if (request.url === "/sso") {
      response.end(
        '<form action="/callback" method="post"></form><script>document.forms[0].submit()</script><script src="/slow"></script>',
      );
    } else if (request.url === "/callback") {
      response.end(
        '<script>location.replace("/landing")</script><script src="/slow"></script>',
      );
    } else if (request.url === "/slow") {
      // The replaced document never reaches DOMContentLoaded.
      response.write("/* waiting */");
    } else response.end('<title>Fixture</title><h1 id="ready">已就绪</h1>');
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const signal = AbortSignal.timeout(20000),
    scope = runtime.scope(signal);
  try {
    const tab = await scope.open(
      `http://127.0.0.1:${(server.address() as AddressInfo).port}/sso`,
      5000,
    );
    await tab.waitForFunction(
      () =>
        location.pathname === "/landing" && !!document.getElementById("ready"),
      undefined,
      { timeout: 5000, signal },
    );
    await tab.setContent(
      '<title>安全知识考试</title><h1>考试</h1><div class="question-type-item"><h2>请完成验证是什么意思？</h2><div class="stem-content-main">短信验证码、安全验证、同时学习、课程已下架</div></div><p>人机验证与短信验证码课程说明</p>',
    );
    await inspectAccess(tab, signal);
    await tab.setContent(
      '<title>Player fixture</title><div style="height:1200px"></div><button id="play" onclick="this.dataset.trusted=String(event.isTrusted)">播放</button>',
    );
    const play = await tab.waitForSelector("#play", { timeout: 5000, signal });
    await clickPlayerControl(tab, play, signal, () => signal.throwIfAborted());
    assert.equal(
      await play.evaluate((e) => (e as HTMLElement).dataset.trusted),
      "true",
    );
    await tab.setContent(
      '<title>考试</title><div role="alert">请输入验证码</div>',
    );
    await assert.rejects(inspectAccess(tab, signal), AuthenticationError);
    await tab.setContent('<title>考试</title><div id="captcha">验证控件</div>');
    await assert.rejects(inspectAccess(tab, signal), AuthenticationError);
    await tab.setContent(
      '<title>课程</title><div role="dialog">您正在学习其他课程</div>',
    );
    await assert.rejects(inspectAccess(tab, signal), /其他学习会话/);
    await tab.setContent(
      '<title>课程</title><div class="ant-result">当前账号没有权限</div>',
    );
    await assert.rejects(inspectAccess(tab, signal), ReviewRequired);
    await tab.setContent(
      "<title>知识中心</title><div><p>您没有权限查看该资源</p><button>关闭</button></div>",
    );
    await assert.rejects(inspectAccess(tab, signal), ReviewRequired);
    await tab.setContent(
      '<title>课程</title><div id="captcha" style="display:none">验证码</div><h1>课程内容</h1>',
    );
    await inspectAccess(tab, signal);
  } finally {
    await scope.close();
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) =>
      server.close((e) => (e ? reject(e) : resolve())),
    );
  }
}
