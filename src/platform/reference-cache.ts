import { lstat, readFile } from "node:fs/promises";
import { join, extname } from "node:path";
import { createHash } from "node:crypto";
import { atomicJson, atomicText, readJson } from "../storage/files.ts";
import { redact } from "../log.ts";

export interface ReferenceFile {
  course: string;
  resource: string;
  path: string;
  bytes?: number;
  sha256?: string;
  kind?: "guide";
  markdown?: string;
  markdownBytes?: number;
  markdownSha256?: string;
}
interface Failure {
  course: string;
  resource?: string;
  message: string;
}
export const checksum = (bytes: Buffer | string) =>
  createHash("sha256").update(bytes).digest("hex");

/** Successful files survive partial runs; only validated files can be reused. */
export class ReferenceCache {
  readonly manifest: {
    files: ReferenceFile[];
    failures: Failure[];
    complete: boolean;
  } = {
    files: [],
    failures: [],
    complete: false,
  };
  readonly output: string;
  constructor(output: string) {
    this.output = output;
  }
  async load(
    validateDocument: (bytes: Buffer, ext: string) => unknown,
    maxBytes: number,
  ) {
    const previous = (await readJson(join(this.output, "manifest.json"))) as
      typeof this.manifest | null;
    if (!previous) return;
    if (!Array.isArray(previous.files) || !Array.isArray(previous.failures))
      throw new Error("资料清单格式损坏，请保留文件后检查");
    this.manifest.failures = previous.failures.filter(
      (f) => f && typeof f.course === "string" && typeof f.message === "string",
    );
    const read = async (name: string, size?: number, sha?: string) => {
      if (
        typeof name !== "string" ||
        !name ||
        /[<>:"/\\|?*\x00-\x1f]/.test(name) ||
        /[. ]$/.test(name)
      )
        throw new Error("资料文件名无效");
      const path = join(this.output, name),
        stats = await lstat(path);
      if (
        !stats.isFile() ||
        stats.isSymbolicLink() ||
        stats.size > maxBytes ||
        (size !== undefined && stats.size !== size)
      )
        throw new Error("资料大小不匹配");
      const bytes = await readFile(path);
      if (sha !== undefined && checksum(bytes) !== sha)
        throw new Error("资料内容校验失败");
      return bytes;
    };
    for (const file of previous.files) {
      if (
        !file ||
        typeof file.course !== "string" ||
        typeof file.resource !== "string"
      )
        throw new Error("资料清单标识损坏，请保留文件后检查");
      try {
        const bytes = await read(file.path, file.bytes, file.sha256);
        if (file.kind === "guide") {
          if (!Array.isArray(JSON.parse(bytes.toString())))
            throw new Error("导学内容不完整");
          const markdown = await read(
            file.markdown!,
            file.markdownBytes,
            file.markdownSha256,
          );
          this.put({
            ...file,
            bytes: bytes.length,
            sha256: checksum(bytes),
            markdownBytes: markdown.length,
            markdownSha256: checksum(markdown),
          });
        } else {
          if (!file.sha256 || file.bytes === undefined)
            throw new Error("课件校验信息缺失");
          validateDocument(bytes, extname(file.path));
          this.put(file);
        }
      } catch {
        this.fail(
          file.course,
          "已有资料缺失或校验不通过，请重新下载",
          file.resource,
        );
      }
    }
  }
  has(course: string, resource: string, kind?: "guide") {
    return this.manifest.files.some(
      (f) => f.course === course && f.resource === resource && f.kind === kind,
    );
  }
  put(file: ReferenceFile) {
    this.manifest.files = this.manifest.files.filter(
      (f) =>
        !(
          f.course === file.course &&
          f.resource === file.resource &&
          f.kind === file.kind
        ),
    );
    this.manifest.files.push(file);
  }
  clearFailure(course: string, resource?: string) {
    this.manifest.failures = this.manifest.failures.filter(
      (f) => f.course !== course || f.resource !== resource,
    );
  }
  fail(course: string, error: unknown, resource?: string) {
    this.clearFailure(course, resource);
    this.manifest.failures.push({ course, resource, message: redact(error) });
    this.manifest.complete = false;
  }
  async save() {
    await atomicJson(join(this.output, "manifest.json"), this.manifest);
    await atomicText(
      join(this.output, "资料索引.md"),
      [
        "# 资料索引",
        "",
        ...this.manifest.files.map((file) => {
          const path = file.markdown || file.path;
          return `- [${path.replace(/[\[\]]/g, "_")}](<${encodeURI(path).replace(/#/g, "%23")}>)`;
        }),
        "",
        `下载失败：${this.manifest.failures.length} 项`,
      ].join("\n"),
    );
  }
}
