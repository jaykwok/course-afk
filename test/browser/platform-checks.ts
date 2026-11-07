import assert from "node:assert/strict";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import type { BrowserRuntime } from "../../src/browser/runtime.ts";
import {
  inspectAccess,
  clickPlayerControl,
  continueArchivedCourse,
  startRegisteredCourse,
} from "../../src/platform/dom.ts";
import { AuthenticationError, ReviewRequired } from "../../src/utils.ts";
import {
  extractQuestions,
  applyAnswers,
  selectedAnswers,
} from "../../src/platform/exam.ts";
import {
  catalogSnapshot,
  extractLinks,
  catalogPageState,
  catalogManifest,
  type DiscoveryContext,
} from "../../src/platform/discovery.ts";

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
  const signal = AbortSignal.timeout(40000),
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
    const course =
      "https://kc.zhixueyun.com/#/study/course/detail/11111111-1111-4111-8111-111111111111";
    const topic = "https://cms.mylearning.cn/topic/catalog.html";
    const article = "https://cms.mylearning.cn/topic/site.html";
    for (const className of ["venues", "arbitrary-layout", ""]) {
      await tab.setContent(
        `<h1>学习专区</h1><a href="${topic}">专题</a><div class="${className}"><a href="${article}">遗址介绍</a><div onclick="window.open('${course}')">课程</div></div>`,
      );
      const catalog = await tab.evaluate(catalogSnapshot);
      assert.deepEqual(extractLinks(catalog.values, catalog.url), [
        topic,
        article,
        course,
      ]);
      await tab.setContent(
        `<title>历史介绍</title><div class="${className}"><h2>历史介绍</h2><p>${"这里是一段历史事件的具体介绍。".repeat(12)}</p></div>`,
      );
      assert.equal(
        catalogPageState(await tab.evaluate(catalogSnapshot)),
        "article",
      );
    }
    await tab.setContent(
      `<title>专题</title><a href="${topic}" onclick="document.body.dataset.navigation='clicked';return false"><span>更多</span></a><a href="${course}">课程</a><button onclick="document.body.dataset.expanded=String(event.isTrusted);this.remove()">加载更多</button>`,
    );
    const manifest = await catalogManifest(tab, {
      lease: { signal },
      auth: {
        assert() {
          signal.throwIfAborted();
        },
      },
    } as unknown as DiscoveryContext);
    assert.equal(manifest.complete, true);
    assert.deepEqual(manifest.links, [topic, course]);
    assert.deepEqual(await tab.evaluate(() => ({ ...document.body.dataset })), {
      expanded: "true",
    });
    await tab.setContent(
      '<title>安全知识考试</title><h1>考试</h1><div class="question-type-item"><h2>请完成验证是什么意思？</h2><div class="stem-content-main">短信验证码、安全验证、同时学习、课程已下架</div></div><p>人机验证与短信验证码课程说明</p>',
    );
    await inspectAccess(tab, signal);
    await tab.setContent(
      `<html><body><div class="question-type-item" data-dynamic-key="q1"><span class="o-score">多选题</span><div class="stem-content-main">选择一和三</div><dl class="preview-list"><dd><label><input type="checkbox"><span class="option-num">A.</span><span class="answer-options">一</span></label></dd><dd><label><input type="checkbox"><span class="option-num">B.</span><span class="answer-options">二</span></label></dd><dd><label><input type="checkbox"><span class="option-num">C.</span><span class="answer-options">三</span></label></dd></dl></div></body></html>`,
    );
    const parsed = await extractQuestions(tab, signal);
    assert.equal(parsed.questions.length, 1);
    assert.equal(parsed.questions[0].type, "multiple");
    const ctx = {
      lease: { signal },
      auth: { assert: () => signal.throwIfAborted() },
    } as unknown as DiscoveryContext;
    assert.equal(
      await applyAnswers(tab, parsed.questions[0], ["A", "C"], ctx),
      true,
    );
    assert.deepEqual(await selectedAnswers(tab, parsed.questions[0], signal), [
      "A",
      "C",
    ]);
    assert.equal(tab.inputBehavior.policy.mode, "human");
    assert.ok(tab.inputBehavior.lastReport?.events);
    await tab.setContent(
      '<title>Player fixture</title><div style="height:1200px"></div><button id="play" onclick="this.dataset.trusted=String(event.isTrusted)">播放</button>',
    );
    const play = await tab.waitForSelector("#play", { timeout: 5000, signal });
    await clickPlayerControl(tab, play, signal, () => signal.throwIfAborted());
    assert.equal(
      await play.evaluate((e) => (e as HTMLElement).dataset.trusted),
      "true",
    );
    await tab.setContent(`<div class="register-mask-layer"><img id="course-registerMask" alt="" style="display:block;width:100px;height:60px;background:blue"
      onclick="document.body.dataset.trusted=String(event.isTrusted);this.parentElement.remove()"></div>`);
    assert.equal(
      await startRegisteredCourse(tab, signal, () => signal.throwIfAborted()),
      true,
    );
    assert.equal(
      await tab.evaluate(() => document.body.dataset.trusted),
      "true",
    );
    assert.equal(await startRegisteredCourse(tab, signal, () => {}), false);
    for (const container of ["ant-modal-content", "study-transition-page"]) {
      await tab.setContent(
        `<div class="${container}">该课程已归档，是否继续学习？<button class="ant-btn" onclick="document.body.dataset.trusted=String(event.isTrusted);this.parentElement.style.display='none'">继续学习</button></div>`,
      );
      assert.equal(
        await continueArchivedCourse(tab, signal, () =>
          signal.throwIfAborted(),
        ),
        true,
      );
      assert.equal(
        await tab.evaluate(() => document.body.dataset.trusted),
        "true",
      );
      assert.equal(await continueArchivedCourse(tab, signal, () => {}), false);
    }
    await tab.setContent(
      '<div class="ant-modal-content">评分<button class="ant-btn" onclick="document.body.dataset.wrong=\'true\'">继续学习</button></div>',
    );
    assert.equal(await continueArchivedCourse(tab, signal, () => {}), false);
    assert.equal(
      await tab.evaluate(() => document.body.dataset.wrong),
      undefined,
    );
    await tab.setContent(
      '<div class="ant-modal-content">已归档<button disabled>继续学习</button></div>',
    );
    assert.equal(await continueArchivedCourse(tab, signal, () => {}), false);
    await tab.setContent(
      '<div class="study-transition-page">课程已归档<button onclick="location.assign(\'/archive-ready\')">继续学习</button></div>',
    );
    assert.equal(
      await continueArchivedCourse(tab, signal, () => signal.throwIfAborted()),
      true,
    );
    assert.equal(new URL(tab.url).pathname, "/archive-ready");
    await assert.rejects(
      startRegisteredCourse(tab, signal, () => {
        throw new Error("凭证已变化");
      }),
      /凭证已变化/,
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
