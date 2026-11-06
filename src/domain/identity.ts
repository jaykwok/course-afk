import { PLATFORM } from "../config.ts";
import { digest } from "../utils.ts";
export const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export type Kind = "course" | "subject" | "exam" | "class" | "entry";
export const CASE_POOL = "https://www.ctexpert.cn/expert-assist-web/casePool";
/** This is a discovery entry, not an additional credential or download domain. */
export function isCasePoolUrl(value: string | URL): boolean {
  try {
    const u = new URL(value);
    return (
      ["http:", "https:"].includes(u.protocol) &&
      ["ctexpert.cn", "www.ctexpert.cn"].includes(u.hostname) &&
      !u.username &&
      !u.password &&
      !u.port &&
      /^\/expert-assist-(?:web|h5)\/casePool\/?$/.test(u.pathname) &&
      (!u.hash || u.hash === "#")
    );
  } catch {
    return false;
  }
}
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
    (!platformHost(url.hostname) && !isCasePoolUrl(url))
  )
    throw new Error("请使用网上大学、知学云或天翼专家案例库链接");
  return url;
}
export function normalize(raw: string): string {
  let value = raw
    .trim()
    .replace(/&amp;/g, "&")
    .replace(/[，。；]+$/, "");
  let url = safeUrl(value);
  for (let n = 0; n < 3; n++) {
    // Discard callback codes and mobile aliases before storing an entry identity.
    if (isCasePoolUrl(url)) return CASE_POOL;
    const params = new URLSearchParams(url.search);
    const append = (query: string) => {
      for (const [key, value] of new URLSearchParams(query))
        params.append(key, value);
    };
    if (url.hash.includes("?"))
      append(url.hash.slice(url.hash.indexOf("?") + 1));
    // paas-container carries its destination's query inside the paasurl value.
    for (const nested of params.getAll("paasurl"))
      if (nested.includes("?")) append(nested.slice(nested.indexOf("?") + 1));
    const get = (...names: string[]) => {
      const values = [...params]
        .filter(([key]) => names.includes(key.toLowerCase()))
        .map(([, value]) => value);
      if (new Set(values).size > 1) throw new Error("链接参数存在冲突");
      return values[0];
    };
    const embedded = get("url", "redirecturl");
    if (embedded?.startsWith("http")) {
      url = safeUrl(embedded);
      continue;
    }
    const route = url.hash.split("?")[0];
    const course = route.match(/^#\/study\/course\/detail\/([\w-]+)(.*)$/);
    if (course) {
      if (!UUID.test(course[1])) throw new Error("平台资源 ID 无效");
      const suffix = course[2].replace(/\/$/, "");
      const context = suffix.match(/^\/([1-9]\d*)\/([01])\/([\w-]+)$/);
      if (suffix && (!context || !UUID.test(context[3])))
        throw new Error("课程来源或必修标记不完整");
      const query = [
        get("type"),
        get("required", "require", "isrequired"),
        get("sourceid"),
      ];
      const path = context ? context.slice(1) : [];
      if (
        path.length &&
        query.some(
          (v, i) =>
            v !== undefined && v.toLowerCase() !== path[i].toLowerCase(),
        )
      )
        throw new Error("课程来源参数与路径不一致");
      const source = path.length ? path : query;
      const base = `${url.origin}/#/study/course/detail/${course[1].toLowerCase()}`;
      if (source.every((v) => v === undefined)) return base;
      if (
        !/^[1-9]\d*$/.test(source[0] || "") ||
        !/^[01]$/.test(source[1] || "") ||
        !UUID.test(source[2] || "")
      )
        throw new Error("课程来源或必修标记不完整");
      return `${base}/${source[0]}/${source[1]}/${source[2]!.toLowerCase()}`;
    }
    const classId = get("classid");
    if (classId !== undefined) return resource("class", classId);
    const id = get("businessid"),
      type = get("businesstype");
    const kind = (
      { "1": "course", "2": "subject", "6": "class" } as Record<string, string>
    )[type || ""];
    if (id && UUID.test(id) && kind) {
      const target = resource(kind as Kind, id);
      if (kind !== "course") return target;
      const source = new URLSearchParams();
      for (const [key, value] of [
        ["type", get("type")],
        ["required", get("required", "require", "isrequired")],
        ["sourceId", get("sourceid")],
      ])
        if (value !== undefined) source.set(key!, value);
      return normalize(`${target}${source.size ? `?${source}` : ""}`);
    }
    break;
  }
  value = url.href.replace("/exam/exam-detail/", "/exam/answer-paper/");
  return value;
}
/** Extract first, then apply exactly the same source and host validation as discovery. */
export function parseLinkInput(text: string) {
  const urls = new Set<string>();
  let rejected = 0;
  for (const match of text
    .replace(/&amp;/g, "&")
    .matchAll(
      /https?:\/\/[^\s<>"'`\\{}()\[\]，。；！？、（）【】《》“”‘’]+/gi,
    )) {
    try {
      urls.add(normalize(match[0].replace(/[.,;!]+$/, "")));
    } catch {
      rejected++;
    }
  }
  return { urls: [...urls], rejected };
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
