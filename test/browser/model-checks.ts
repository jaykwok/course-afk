import assert from "node:assert/strict";
import type { Tab } from "nodriver-ts";
import { readSettings } from "../../src/config.ts";
import { until } from "../../src/utils.ts";
import { modelService } from "../model-service.ts";

export async function modelChecks(page: Tab, settingsPath: string) {
  const provider = await modelService();
  let release = () => {};
  const openSettings = async () => {
    await (await page.query("#settings-open"))!.click();
    await page.waitForFunction(
      () =>
        (document.getElementById("settings-dialog") as HTMLDialogElement).open,
      undefined,
      { timeout: 5000 },
    );
  };
  const waitModels = () =>
    page.waitForFunction(
      () =>
        !(document.getElementById("models-toggle") as HTMLButtonElement)
          .disabled,
      undefined,
      { timeout: 5000 },
    );
  const options = () =>
    page.evaluate(() =>
      [...document.querySelectorAll('#model-options [role="option"]')].map(
        (option) => option.textContent,
      ),
    );
  const value = () =>
    page.evaluate(
      () =>
        (document.getElementById("settings-model") as HTMLInputElement).value,
    );
  try {
    await openSettings();
    await (await page.query("#settings-base-url"))!.fill(provider.baseURL);
    await (await page.query("#settings-api-key"))!.fill(
      "browser-fixture-secret",
    );
    provider.state.hold = new Promise<void>((resolve) => {
      release = resolve;
    });
    await (await page.query("#models-fetch"))!.click();
    await until(async () => provider.requests.length === 1, 5000);
    assert.equal(
      await page.evaluate(
        () =>
          (document.getElementById("models-fetch") as HTMLButtonElement)
            .disabled,
      ),
      true,
    );
    await (await page.query("#settings-api-key"))!.fill("browser-new-secret");
    release();
    provider.state.hold = undefined;
    assert.equal(
      await page.evaluate(
        () =>
          (document.getElementById("models-toggle") as HTMLButtonElement)
            .disabled,
      ),
      true,
      "editing credentials cancels the pending catalog",
    );
    await (await page.query("#models-fetch"))!.click();
    await waitModels();
    assert.deepEqual(await options(), [
      "atlas-chat",
      "atlas-mini",
      "orbit-reasoner",
    ]);
    assert.equal(provider.requests.at(-1)!.auth, "Bearer browser-new-secret");
    assert.equal(
      readSettings(settingsPath).openai.apiKey,
      "",
      "fetching does not save the draft",
    );

    const input = (await page.query("#settings-model"))!;
    await input.fill("ATLAS");
    assert.deepEqual(await options(), ["atlas-chat", "atlas-mini"]);
    await page.screenshot({ path: "build/browser-check/model-search.png" });
    assert.equal(
      await page.evaluate(() => {
        const dialog = document.getElementById("settings-dialog")!;
        return dialog.scrollWidth > dialog.clientWidth;
      }),
      false,
    );
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    assert.equal(await value(), "atlas-mini");
    assert.equal(
      await page.evaluate(
        () =>
          (document.getElementById("settings-dialog") as HTMLDialogElement)
            .open,
      ),
      true,
      "choosing a model does not submit settings",
    );

    await (await page.query("#models-toggle"))!.click();
    assert.equal(
      (await options()).length,
      3,
      "the dropdown can show all models after choosing one",
    );
    await (await page.query("#model-option-2"))!.click();
    assert.equal(await value(), "orbit-reasoner");
    await input.fill("browser-fixture-model");
    assert.deepEqual(await options(), []);
    assert.equal(
      await page.evaluate(
        () => document.getElementById("models-empty")!.hidden,
      ),
      false,
    );
    await page.keyboard.press("Escape");
    assert.equal(
      await page.evaluate(
        () =>
          (document.getElementById("settings-dialog") as HTMLDialogElement)
            .open,
      ),
      true,
      "Escape closes suggestions before closing settings",
    );
    await (await page.query("#settings-save"))!.click();
    await page.waitForFunction(
      () =>
        !(document.getElementById("settings-dialog") as HTMLDialogElement)
          .open &&
        !(document.getElementById("settings-open") as HTMLButtonElement)
          .disabled,
      undefined,
      { timeout: 10000 },
    );
    assert.equal(
      readSettings(settingsPath).openai.model,
      "browser-fixture-model",
    );

    await openSettings();
    assert.equal(
      await page.evaluate(
        () =>
          (document.getElementById("settings-api-key") as HTMLInputElement)
            .value,
      ),
      "",
    );
    await (await page.query("#models-fetch"))!.click();
    await waitModels();
    assert.equal(provider.requests.at(-1)!.auth, "Bearer browser-new-secret");
    assert.equal(
      await value(),
      "browser-fixture-model",
      "fetching preserves a custom model",
    );
    await page.keyboard.press("Escape");
    provider.state.status = 401;
    await (await page.query("#models-fetch"))!.click();
    await page.waitForFunction(
      () =>
        document
          .getElementById("models-status")!
          .textContent!.includes("HTTP 401"),
      undefined,
      { timeout: 5000 },
    );
    assert.equal(await value(), "browser-fixture-model");
    assert.ok(
      !(await page.evaluate(() => document.body.textContent!)).includes(
        "browser-new-secret",
      ),
    );
    provider.state.status = 200;
    provider.state.models = [];
    await (await page.query("#models-fetch"))!.click();
    await waitModels();
    assert.deepEqual(await options(), []);
    await page.keyboard.press("Escape");
    await (await page.query("#settings-close"))!.click();
  } finally {
    release();
    await provider.close();
  }
}
