import assert from "node:assert/strict";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import type { BrowserRuntime } from "../../src/browser/runtime.ts";
import { applyLoginPreferences } from "../../src/browser/login.ts";
import { until } from "../../src/utils.ts";

export async function loginChecks(runtime: BrowserRuntime) {
  const server = createServer((request, response) => {
    response.setHeader("Content-Type", "text/html; charset=utf-8");
    if (request.url !== "/frame") {
      response.end(
        `<iframe id="esurfingloginiframe" style="width:700px;height:450px" src="http://localhost:${(server.address() as AddressInfo).port}/frame"></iframe>`,
      );
      return;
    }
    response.end(`<!doctype html><style>
      i,[id^=j-agreement-box]{display:inline-block;width:22px;height:22px;border:1px solid;cursor:pointer}
      .login-select{position:relative;display:inline-block;padding:8px}
      .login-option-list{display:none;position:absolute;top:100%;left:0;background:white;width:160px;z-index:10}
      .login-select:hover .login-option-list{display:block}.option{padding:6px;margin:0}
      [hidden]{display:none!important}
    </style>
    <div id="modes"></div><button id="reset">重新渲染</button><main></main>
    <script>
    let trusted=[], active='-qr'; const modes=['-qr','','-sms','-qk'];
    function draw(){document.querySelector('main').innerHTML=modes.map((suffix)=>
      '<section data-mode="'+suffix+'" '+(suffix===active?'':'hidden')+'><div id="j-auto-group'+suffix+'">'+
      '<i id="j-auto-login'+suffix+'" class="checkbox-icon1" data-index="0"></i>'+
      '<span class="login-select"><em class="login-time-text">7天内自动登录</em><div class="login-option-list">'+
      '<p class="option active" data-time="1">7天内自动登录</p><p class="option" data-time="3">30天内自动登录</p></div></span></div>'+
      '<span id="j-agreement-box'+suffix+'" class="checkbox-icon1"></span></section>').join('');}
    document.querySelector('#modes').innerHTML=modes.map((m,i)=>'<button data-switch="'+m+'">方式'+i+'</button>').join('');
    document.addEventListener('click',e=>{const el=e.target;
      if(el.hasAttribute('data-switch')){active=el.dataset.switch;document.querySelectorAll('section').forEach(s=>s.hidden=s.dataset.mode!==active);return;}
      if(el.id==='reset'){draw();return;}
      if(el.classList.contains('option')){trusted.push(e.isTrusted);const g=el.closest('[id^=j-auto-group]');g.querySelectorAll('.option').forEach(o=>o.classList.toggle('active',o===el));g.querySelector('.login-time-text').textContent=el.textContent;}
      if(el.id.startsWith('j-auto-login')||el.id.startsWith('j-agreement-box')){trusted.push(e.isTrusted);const checked=el.dataset.index!=='1';el.dataset.index=checked?'1':'0';el.className=checked?'checkbox-icon2':'checkbox-icon1';}
    });draw();
    </script>`);
  });
  await new Promise<void>((resolve) => server.listen(0, resolve));
  const signal = AbortSignal.timeout(60000),
    scope = runtime.scope(signal);
  try {
    const tab = await scope.open(
      `http://127.0.0.1:${(server.address() as AddressInfo).port}/`,
    );
    await tab.waitForFunction(
      () => !!document.getElementById("esurfingloginiframe"),
      undefined,
      { signal, timeout: 5000 },
    );
    let frame = await until(
      async () => tab.frames.find((f) => f.url.endsWith("/frame")),
      5000,
      signal,
    );
    await frame.waitForSelector("#j-auto-group-qr", { signal, timeout: 5000 });
    for (const mode of ["-qr", "", "-sms", "-qk"]) {
      await (await frame.query(`[data-switch="${mode}"]`))!.click({ signal });
      await frame.waitForFunction(
        (suffix) =>
          !document.querySelector<HTMLElement>(
            `section[data-mode="${suffix}"]`,
          )!.hidden,
        mode,
        { signal, timeout: 5000 },
      );
      const check = () =>
        frame!.evaluate((suffix) => {
          const group = document.getElementById("j-auto-group" + suffix)!;
          return {
            duration: group
              .querySelector(".option.active")
              ?.getAttribute("data-time"),
            text: group.querySelector(".login-time-text")?.textContent,
            checked: group.querySelector("i")?.getAttribute("data-index"),
            agreement: document
              .getElementById("j-agreement-box" + suffix)
              ?.getAttribute("data-index"),
          };
        }, mode);
      const expected = {
        duration: "3",
        text: "30天内自动登录",
        checked: "1",
        agreement: "1",
      };
      await until(
        async () => {
          await applyLoginPreferences(tab, signal);
          return JSON.stringify(await check()) === JSON.stringify(expected);
        },
        10000,
        signal,
      );
      assert.deepEqual(await check(), expected);
      const count: number = await frame.evaluate<number>("trusted.length");
      await applyLoginPreferences(tab, signal);
      assert.equal(
        await frame.evaluate("trusted.length"),
        count,
        "already selected options must not be toggled",
      );
    }
    await (await frame.query("#reset"))!.click({ signal });
    await applyLoginPreferences(tab, signal);
    assert.equal(
      await frame.evaluate(() =>
        document
          .querySelector("#j-auto-group-qk .option.active")
          ?.getAttribute("data-time"),
      ),
      "3",
    );
    assert.equal(await frame.evaluate("trusted.every(Boolean)"), true);
    await tab.reload({ waitUntil: "domcontentloaded", signal, timeout: 10000 });
    frame = await until(
      async () => tab.frames.find((f) => f.url.endsWith("/frame")),
      5000,
      signal,
    );
    await frame.waitForSelector("#j-auto-group-qr", { signal, timeout: 5000 });
    await applyLoginPreferences(tab, signal);
    assert.equal(
      await frame.evaluate(() =>
        document
          .querySelector("#j-auto-group-qr .option.active")
          ?.getAttribute("data-time"),
      ),
      "3",
    );
  } finally {
    await scope.close();
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) =>
      server.close((e) => (e ? reject(e) : resolve())),
    );
  }
}
