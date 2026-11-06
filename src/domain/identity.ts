import { PLATFORM } from "../config.ts";
import { digest } from "../utils.ts";
export const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export type Kind = "course" | "subject" | "exam" | "class" | "entry";
export function platformHost(host: string) {
  return ["mylearning.cn", "zhixueyun.com"].some(
    (d) => host === d || host.endsWith(`.${d}`),
  );
}
export function safeUrl(raw: string): URL {
  const url = new URL(raw);
  if (
    !["https:", "http:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    (url.port && !["443", "80"].includes(url.port)) ||
    !platformHost(url.hostname)
  )
    throw new Error("请使用网上大学或知学云平台链接");
  return url;
}
export function normalize(raw: string): string {
  let value = raw
    .trim()
    .replace(/&amp;/g, "&")
    .replace(/[，。；]+$/, "");
  let url = safeUrl(value);
  for (let n = 0; n < 3; n++) {
    const params = new URLSearchParams(
      url.search ||
        (url.hash.includes("?") ? url.hash.slice(url.hash.indexOf("?")) : ""),
    );
    const embedded = params.get("url") || params.get("redirectUrl");
    if (embedded?.startsWith("http")) {
      url = safeUrl(embedded);
      continue;
    }
    const id = params.get("businessId"),
      type = params.get("businessType");
    const kind = (
      { "1": "course", "2": "subject", "6": "class" } as Record<string, string>
    )[type || ""];
    if (id && UUID.test(id) && kind) return resource(kind as Kind, id);
    break;
  }
  value = url.href.replace("/exam/exam-detail/", "/exam/answer-paper/");
  return value;
}
export function resource(
  kind: Kind,
  id: string,
  subject?: string,
  required?: string | number,
): string {
  if (!UUID.test(id)) throw new Error("平台资源 ID 无效");
  const route =
    kind === "exam"
      ? "/exam/answer-paper/"
      : kind === "class"
        ? "/train-new/class-detail/"
        : `/study/${kind}/detail/`;
  const source = subject ? `/6/${required}/${subject.toLowerCase()}` : "";
  if (
    subject &&
    (!UUID.test(subject) || !["0", "1"].includes(String(required)))
  )
    throw new Error("课程来源或必修标记不完整");
  return `${PLATFORM}/#${route}${id.toLowerCase()}${source}`;
}
export interface Entry {
  id: string;
  account: string;
  kind: Kind;
  url: string;
  businessId: string;
  sourceType?: string;
  sourceId?: string;
  required?: string;
}
export function entry(raw: string, account: string): Entry {
  const url = normalize(raw),
    parsed = new URL(url);
  const m = parsed.hash
    .split("?")[0]
    .match(
      /^#\/(?:study\/(course|subject)\/detail|exam\/(answer-paper)|train-new\/(class-detail))\/([\w-]+)(?:\/(\d+)\/([01])\/([\w-]+))?$/,
    );
  const kind: Kind = m ? (m[1] as Kind) || (m[2] ? "exam" : "class") : "entry";
  if (m && !UUID.test(m[4])) throw new Error("平台资源 ID 无效");
  const businessId = m ? m[4].toLowerCase() : digest(url);
  const identity = [
    parsed.hostname,
    account,
    kind,
    businessId,
    m?.[5] || "",
    m?.[6] || "",
    m?.[7]?.toLowerCase() || "",
  ];
  return {
    id: digest(JSON.stringify(identity)),
    account,
    kind,
    url,
    businessId,
    sourceType: m?.[5],
    required: m?.[6],
    sourceId: m?.[7]?.toLowerCase(),
  };
}
export const accountId = (name: string) => (name ? digest(name) : "unbound");
