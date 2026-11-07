import OpenAI from "openai";
import type { Settings } from "../config.ts";

export async function listModels(
  settings: Pick<Settings["openai"], "baseURL" | "apiKey" | "timeout">,
  signal?: AbortSignal,
): Promise<string[]> {
  const client = new OpenAI({
    baseURL: settings.baseURL,
    apiKey: settings.apiKey,
    timeout: settings.timeout * 1000,
    maxRetries: 0,
  });
  try {
    const page = await client.models.list({ signal });
    return page.data.map((model) => model.id).sort();
  } catch (error) {
    if (error instanceof OpenAI.APIConnectionTimeoutError)
      throw new Error("获取模型超时，请稍后重试。");
    if (error instanceof OpenAI.APIConnectionError)
      throw new Error("无法连接 AI 接口，请检查接口地址和网络。");
    if (error instanceof OpenAI.APIError) {
      const hint =
        error.status === 401 || error.status === 403
          ? "请检查 API Key 和访问权限。"
          : error.status === 404
            ? "该地址未提供模型列表，请检查接口地址，或手动填写模型名称。"
            : error.status === 429
              ? "请求过于频繁，请稍后重试。"
              : "请稍后重试，或手动填写模型名称。";
      throw new Error(`获取模型失败（HTTP ${error.status}）。${hint}`);
    }
    throw new Error("无法读取接口返回的模型列表，可手动填写模型名称。");
  }
}
