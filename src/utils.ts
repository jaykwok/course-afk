import { setTimeout as delay } from "node:timers/promises";
import { randomBytes, createHash } from "node:crypto";
export const token = () => randomBytes(32).toString("hex");
export const digest = (value: string) =>
  createHash("sha256").update(value).digest("hex");
export const sleep = (ms: number, signal?: AbortSignal) =>
  delay(ms, undefined, { signal });
export function check(signal?: AbortSignal) {
  signal?.throwIfAborted();
}
export async function until<T>(
  fn: () => Promise<T>,
  timeout: number,
  signal?: AbortSignal,
  interval = 250,
): Promise<Exclude<T, null | undefined | false | 0 | "">> {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    check(signal);
    const result = await fn();
    if (result) return result as Exclude<T, null | undefined | false | 0 | "">;
    await sleep(interval, signal);
  }
  throw new Error("等待平台响应超时");
}
export function object(value: unknown): Record<string, any> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, any>)
    : {};
}
export function data(value: unknown): Record<string, any> {
  const v = object(value);
  return object(v.data ?? v);
}
export function list(value: unknown): any[] {
  if (Array.isArray(value)) return value;
  const v = object(value);
  const result = v.items ?? v.datas ?? v.data ?? v.chapters;
  if (!Array.isArray(result)) throw new Error("平台响应缺少完整列表");
  return result;
}
export class ReviewRequired extends Error {
  readonly reason: string;
  constructor(reason: string, message: string) {
    super(message);
    this.reason = reason;
  }
}
export class AuthenticationError extends Error {}
