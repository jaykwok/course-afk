import { existsSync, readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { atomicJson } from "./storage/files.ts";

export const ROOT = resolve(
  dirname(fileURLToPath(import.meta.url)),
  import.meta.url.includes("/dist/") ? "../.." : "..",
);
export interface Settings {
  version: 1;
  browser: { executable: string };
  learning: { watchSlice: number; stallLimit: number; sectionTimeout: number };
  openai: {
    baseURL: string;
    apiKey: string;
    model: string;
    protocol: "chat" | "responses";
    output: "prompt_json" | "json_schema";
    timeout: number;
    totalTimeout: number;
    retries: number;
  };
}
export function defaults(): Settings {
  return {
    version: 1,
    browser: { executable: "" },
    learning: { watchSlice: 20, stallLimit: 300, sectionTimeout: 14400 },
    openai: {
      baseURL: "https://api.openai.com/v1",
      apiKey: "",
      model: "",
      protocol: "responses",
      output: "prompt_json",
      timeout: 60,
      totalTimeout: 180,
      retries: 2,
    },
  };
}
export function validateSettings(input: unknown): Settings {
  if (!input || typeof input !== "object" || Array.isArray(input))
    throw new Error("设置格式无效");
  const v = input as Settings;
  if (v.version !== 1 || !v.browser || !v.learning || !v.openai)
    throw new Error("设置文件结构不完整");
  const string = (value: unknown, label: string, max: number) => {
    if (
      typeof value !== "string" ||
      value.length > max ||
      /[\x00-\x1f]/.test(value)
    )
      throw new Error(`${label}格式无效`);
    return value.trim();
  };
  const number = (value: unknown, label: string, min: number, max: number) => {
    if (
      typeof value !== "number" ||
      !Number.isInteger(value) ||
      value < min ||
      value > max
    )
      throw new Error(`${label}必须是 ${min}–${max} 之间的整数`);
    return value;
  };
  const baseURL = string(v.openai.baseURL, "接口地址", 2048);
  let url: URL;
  try {
    url = new URL(baseURL);
  } catch {
    throw new Error("OpenAI 接口地址格式无效");
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  )
    throw new Error("OpenAI 接口地址格式无效");
  if (!["chat", "responses"].includes(v.openai.protocol))
    throw new Error("仅支持 Chat Completions / Responses 协议");
  if (!["prompt_json", "json_schema"].includes(v.openai.output))
    throw new Error("答案格式无效");
  return {
    version: 1,
    browser: { executable: string(v.browser.executable, "浏览器路径", 4096) },
    learning: {
      watchSlice: number(v.learning.watchSlice, "进度检查间隔", 1, 120),
      stallLimit: number(v.learning.stallLimit, "播放停滞期限", 30, 3600),
      sectionTimeout: number(v.learning.sectionTimeout, "章节期限", 60, 86400),
    },
    openai: {
      baseURL,
      apiKey: string(v.openai.apiKey, "API Key", 4096),
      model: string(v.openai.model, "模型名称", 256),
      protocol: v.openai.protocol,
      output: v.openai.output,
      timeout: number(v.openai.timeout, "请求超时", 1, 3600),
      totalTimeout: number(v.openai.totalTimeout, "总期限", 1, 3600),
      retries: number(v.openai.retries, "重试次数", 0, 5),
    },
  };
}
export const SETTINGS_FILE = resolve(ROOT, "data/settings.json");
export function readSettings(path = SETTINGS_FILE): Settings {
  if (!existsSync(path)) return defaults();
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, "utf8"));
  } catch {
    throw new Error("设置文件无法读取，请检查 data/settings.json");
  }
  return validateSettings(parsed);
}
export function publicSettings(path: string) {
  const s = readSettings(path);
  return {
    ...s,
    openai: { ...s.openai, apiKey: "", keyConfigured: !!s.openai.apiKey },
  };
}
export function prepareSettings(
  input: unknown,
  path: string,
  clearKey = false,
): Settings {
  const settings = validateSettings(input);
  settings.openai.apiKey = clearKey
    ? ""
    : settings.openai.apiKey || readSettings(path).openai.apiKey;
  return settings;
}
export async function saveSettings(path: string, settings: Settings) {
  await atomicJson(path, validateSettings(settings));
}
export function configuration(root = ROOT, dataDir = resolve(root, "data")) {
  const settingsPath = resolve(dataDir, "settings.json"),
    s = readSettings(settingsPath);
  return {
    root,
    dataDir,
    logsDir: resolve(root, "logs"),
    settingsPath,
    browserPath: s.browser.executable || undefined,
    ...s.learning,
  };
}
export type Configuration = ReturnType<typeof configuration>;
export const PLATFORM = "https://kc.zhixueyun.com";
export const HOME = "https://www.mylearning.cn/p5/index.html";
export const CENTER = "https://center.mylearning.cn";
