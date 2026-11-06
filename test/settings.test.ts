import test from "node:test";
import assert from "node:assert/strict";
import { join } from "node:path";
import { writeFile } from "node:fs/promises";
import {
  defaults,
  readSettings,
  prepareSettings,
  publicSettings,
  saveSettings,
  configuration,
} from "../src/config.ts";
import { temporary, application } from "./helpers.ts";
import { token } from "../src/utils.ts";

test("settings persist in one file; secret is write-only in projection and blank preserves it", async () => {
  const t = await temporary(),
    path = join(t.dir, "settings.json");
  try {
    const s = defaults();
    s.openai.apiKey = "private-test-key";
    s.openai.model = "model-a";
    await saveSettings(path, s);
    assert.equal(readSettings(path).openai.apiKey, s.openai.apiKey);
    const shown = publicSettings(path);
    assert.equal(shown.openai.keyConfigured, true);
    assert.equal(JSON.stringify(shown).includes(s.openai.apiKey), false);
    shown.openai.model = "model-b";
    await saveSettings(path, prepareSettings(shown, path));
    assert.equal(readSettings(path).openai.apiKey, s.openai.apiKey);
    assert.equal(readSettings(path).openai.model, "model-b");
    await saveSettings(path, prepareSettings(shown, path, true));
    assert.equal(publicSettings(path).openai.keyConfigured, false);
    assert.throws(() =>
      prepareSettings(
        { ...shown, learning: { ...shown.learning, watchSlice: 0 } },
        path,
      ),
    );
    assert.throws(() =>
      prepareSettings(
        { ...shown, openai: { ...shown.openai, protocol: "vendor" } },
        path,
      ),
    );
    assert.throws(() =>
      prepareSettings(
        {
          ...shown,
          openai: {
            ...shown.openai,
            baseURL: "https://user:secret@example.test",
          },
        },
        path,
      ),
    );
    await writeFile(path, "{bad json");
    assert.throws(() => readSettings(path), /设置文件无法读取/);
  } finally {
    await t.close();
  }
});
test("settings can be saved before platform login and do not project API keys into snapshots", async () => {
  const f = await application();
  try {
    const settings = defaults();
    settings.openai.apiKey = "private-test-key";
    settings.openai.model = "model";
    settings.learning.watchSlice = 12;
    f.app.command(token(), "operation", { kind: "settings", settings });
    await f.app.settled();
    assert.equal(
      readSettings(f.app.config.settingsPath).openai.apiKey,
      settings.openai.apiKey,
    );
    assert.equal(f.app.config.watchSlice, 12);
    assert.equal(f.app.dashboard().ai_configured, true);
    assert.equal(
      JSON.stringify(f.app.snapshot()).includes(settings.openai.apiKey),
      false,
    );
    const next = configuration(f.app.config.root, f.app.config.dataDir);
    assert.equal(next.watchSlice, 12);
    assert.equal(f.runtime.management, undefined);
  } finally {
    await f.close();
  }
});
