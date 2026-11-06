import { mkdir, writeFile, rename, rm } from "node:fs/promises";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { PLATFORM } from "../config.ts";
import { platformHost, entry } from "../domain/identity.ts";
import { PlatformHttp } from "../browser/http.ts";
import { atomicJson } from "../storage/files.ts";
import type { Task } from "../storage/repository.ts";
import { redact } from "../log.ts";
import { AuthenticationError, list, token } from "../utils.ts";
import { subjectManifest, type DiscoveryContext } from "./discovery.ts";
import { courseInfo } from "./learning.ts";
export const MAX_BYTES = 128 * 1024 * 1024;
export function filename(name: string) {
  const s = name
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, "_")
    .replace(/[. ]+$/, "")
    .slice(0, 100);
  return !s || /^(?:CON|PRN|AUX|NUL|COM\d|LPT\d)(?:\.|$)/i.test(s)
    ? `_${s || "资料"}`
    : s;
}
export function previewUrl(
  raw: string,
  base = "https://dianxinsafecdn.zhixueyun.com",
) {
  if (/[\\\x00-\x20]/.test(raw)) throw new Error("课件地址格式错误");
  const u = new URL(raw, raw.startsWith("/api/") ? PLATFORM : base);
  if (
    u.protocol !== "https:" ||
    !platformHost(u.hostname) ||
    u.username ||
    u.password ||
    (u.port && u.port !== "443")
  )
    throw new Error("课件地址不在允许范围");
  return u;
}
export function decodeDocument(
  raw: Buffer,
  ext: string,
): { bytes: Buffer; ext: string } {
  if (raw.length > MAX_BYTES) throw new Error("课件超过 128 MiB");
  if (raw.subarray(0, 20).toString().trimStart().startsWith("JVBER")) {
    const text = raw.toString().replace(/\s/g, "");
    if (!/^[A-Za-z0-9+/]*={0,2}$/.test(text)) throw new Error("PDF 编码无效");
    raw = Buffer.from(text, "base64");
  }
  if (raw.subarray(0, 5).toString() === "%PDF-")
    return { bytes: raw, ext: "pdf" };
  ext = ext.toLowerCase().replace(/^\./, "");
  if (
    ["doc", "ppt"].includes(ext) &&
    raw.subarray(0, 8).equals(Buffer.from("d0cf11e0a1b11ae1", "hex"))
  )
    return { bytes: raw, ext };
  if (
    ["docx", "pptx"].includes(ext) &&
    raw.length >= 4 &&
    raw.readUInt32LE(0) === 0x04034b50
  ) {
    const eocd = raw.lastIndexOf(Buffer.from("504b0506", "hex"));
    if (eocd < 0 || eocd + 22 > raw.length)
      throw new Error("Office 压缩文件不完整");
    let offset = raw.readUInt32LE(eocd + 16),
      found = false;
    const count = raw.readUInt16LE(eocd + 10);
    for (let i = 0; i < count; i++) {
      if (offset + 46 > raw.length || raw.readUInt32LE(offset) !== 0x02014b50)
        throw new Error("Office 目录损坏");
      const len = raw.readUInt16LE(offset + 28),
        extra = raw.readUInt16LE(offset + 30),
        comment = raw.readUInt16LE(offset + 32);
      const name = raw.subarray(offset + 46, offset + 46 + len).toString();
      if (
        name === (ext === "docx" ? "word/document.xml" : "ppt/presentation.xml")
      )
        found = true;
      offset += 46 + len + extra + comment;
    }
    if (found) return { bytes: raw, ext };
  }
  throw new Error("下载内容不是受支持的 PDF / Office 课件");
}
export async function downloadFile(
  raw: string,
  authorization: string,
  signal: AbortSignal,
  fetcher: typeof fetch = fetch,
) {
  let url = previewUrl(raw);
  const combined = AbortSignal.any([signal, AbortSignal.timeout(120000)]);
  for (let n = 0; n < 6; n++) {
    const response = await fetcher(url, {
      redirect: "manual",
      signal: combined,
      headers: {
        Accept: "*/*",
        ...(url.origin === PLATFORM
          ? { Authorization: authorization, Version: "12.1.1" }
          : {}),
      },
    });
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      const target = response.headers.get("location");
      await response.body?.cancel();
      if (!target) throw new Error("课件重定向目标缺失");
      url = previewUrl(new URL(target, url).href);
      continue;
    }
    if (!response.ok) {
      await response.body?.cancel();
      throw new Error(`课件下载失败 HTTP ${response.status}`);
    }
    if (Number(response.headers.get("content-length")) > MAX_BYTES) {
      await response.body?.cancel();
      throw new Error("课件超过 128 MiB");
    }
    const reader = response.body?.getReader();
    if (!reader) throw new Error("课件内容为空");
    const chunks: Buffer[] = [];
    let size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_BYTES) throw new Error("课件超过 128 MiB");
        chunks.push(Buffer.from(value));
      }
    } finally {
      await reader.cancel();
    }
    return Buffer.concat(chunks);
  }
  throw new Error("课件重定向过多");
}
export async function references(
  task: Task,
  ctx: DiscoveryContext,
  directory: string,
) {
  const root = await ctx.auth.navigate(ctx.scope, task.url, ctx.lease),
    http = new PlatformHttp(root, ctx.auth, ctx.lease);
  const output = join(directory, "references", task.account, task.businessId);
  await mkdir(output, { recursive: true });
  const manifest: { files: any[]; failures: any[]; complete: boolean } = {
    files: [],
    failures: [],
    complete: true,
  };
  try {
    const urls =
      task.kind === "course"
        ? [task.url]
        : task.kind === "subject"
          ? subjectManifest(
              await http.json(
                `/api/v1/course-study/subject/chapter-progress?courseId=${task.businessId}&knowledgePaymentEnable=false`,
              ),
              task,
            ).links.filter((url) => entry(url, task.account).kind === "course")
          : [];
    if (!urls.length) throw new Error("请使用课程或包含课程的主题链接下载资料");
    for (const url of urls) {
      ctx.auth.assert(ctx.lease);
      const course = ctx.repo.upsert(url, task.account),
        tab = await ctx.auth.navigate(ctx.scope, url, ctx.lease);
      const api = new PlatformHttp(tab, ctx.auth, ctx.lease);
      try {
        const { info, sections } = await courseInfo(api, course);
        const resources = [
          ...sections,
          ...(info.courseAttachments || []).map((a: any) => ({
            ...a,
            attachmentId: a.attachmentId || a.id,
            fileType: String(a.name || "")
              .split(".")
              .at(-1),
          })),
        ];
        const seen = new Set<string>();
        for (const s of resources) {
          const id = String(s.attachmentId || s.resourceId || "");
          if (!id || seen.has(id)) continue;
          seen.add(id);
          const ext = String(s.fileType || "").toLowerCase();
          try {
            ctx.auth.assert(ctx.lease);
            if (["pdf", "doc", "docx", "ppt", "pptx"].includes(ext)) {
              const preview = await api.json(
                `/api/v1/tools-center-v2/file-cloud/preview?id=${encodeURIComponent(id)}`,
              );
              if (typeof preview.url !== "string")
                throw new Error("课件预览地址缺失");
              const auth = await tab.evaluate(
                () => {
                  const c = document.cookie.match(
                    /(?:^|; )authorization=([^;]+)/,
                  )?.[1];
                  if (c) return decodeURIComponent(c);
                  try {
                    const t = JSON.parse(
                      localStorage.getItem("token") || "{}",
                    ).access_token;
                    if (t) return `Bearer__${t}`;
                  } catch {}
                  const s = localStorage.getItem("saas-token");
                  return s ? `Bearer__${s}` : "";
                },
                undefined,
                { timeout: 5000, signal: ctx.lease.signal },
              );
              ctx.auth.assert(ctx.lease);
              const file = decodeDocument(
                await downloadFile(preview.url, auth, ctx.lease.signal),
                preview.extention || preview.type || ext,
              );
              ctx.auth.assert(ctx.lease);
              const name = `${filename(info.name || course.businessId)}-${filename(s.name || id)}-${filename(id.slice(0, 8))}.${file.ext}`,
                path = join(output, name),
                tmp = `${path}.${token()}.tmp`;
              try {
                await writeFile(tmp, file.bytes, { flag: "wx" });
                await rename(tmp, path);
              } finally {
                await rm(tmp, { force: true });
              }
              manifest.files.push({
                course: course.businessId,
                resource: id,
                path: name,
                bytes: file.bytes.length,
                sha256: createHash("sha256").update(file.bytes).digest("hex"),
              });
              ctx.report(`已保存课件：${name}`);
            }
            if (s.guideStudyFlag) {
              const items = list(
                await api.json(
                  `/api/v1/course-study/guide-study/get-guide-study-info?courseId=${course.businessId}&attachmentId=${encodeURIComponent(id)}`,
                ),
              );
              const name = `${filename(info.name || course.businessId)}-${filename(id.slice(0, 8))}-导学.json`;
              await atomicJson(join(output, name), items);
              const markdown = name.replace(/\.json$/, ".md");
              await writeFile(
                join(output, markdown),
                [
                  `# ${info.name || course.businessId} · ${s.name || "导学"}`,
                  "",
                  ...items.flatMap((item) => [
                    `## ${String(item.name || "总述")}`,
                    "",
                    String(item.content || "暂无导学内容"),
                    "",
                  ]),
                ].join("\n"),
              );
              manifest.files.push({
                course: course.businessId,
                resource: id,
                path: name,
                markdown,
                kind: "guide",
              });
            }
          } catch (e) {
            if (ctx.lease.signal.aborted || e instanceof AuthenticationError)
              throw e;
            manifest.failures.push({
              course: course.businessId,
              resource: id,
              message: redact(e),
            });
            manifest.complete = false;
          }
          await atomicJson(join(output, "manifest.json"), manifest);
        }
      } catch (e) {
        if (ctx.lease.signal.aborted || e instanceof AuthenticationError)
          throw e;
        manifest.failures.push({
          course: course.businessId,
          message: redact(e),
        });
        manifest.complete = false;
      } finally {
        await tab.close();
      }
    }
    await atomicJson(join(output, "manifest.json"), manifest);
    await writeFile(
      join(output, "资料索引.md"),
      [
        "# 资料索引",
        "",
        ...manifest.files.map(
          (file) =>
            `- [${file.markdown || file.path}](<${encodeURI(file.markdown || file.path)}>)`,
        ),
        "",
        `下载失败：${manifest.failures.length} 项`,
      ].join("\n"),
    );
    return manifest;
  } finally {
    await root.close();
  }
}
