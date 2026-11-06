import { mkdir, open, rename, rm, readFile } from "node:fs/promises";
import { dirname } from "node:path";
import { token } from "../utils.ts";
export async function atomicJson(path: string, data: unknown) {
  await atomicText(path, JSON.stringify(data, null, 2));
}
export async function atomicText(path: string, data: string) {
  await mkdir(dirname(path), { recursive: true });
  const tmp = `${path}.${token()}.tmp`;
  try {
    const file = await open(tmp, "wx", 0o600);
    try {
      await file.writeFile(data, "utf8");
      await file.sync();
    } finally {
      await file.close();
    }
    await rename(tmp, path);
  } finally {
    await rm(tmp, { force: true });
  }
}
export async function readJson(path: string): Promise<unknown> {
  try {
    return JSON.parse(await readFile(path, "utf8"));
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw new Error("数据文件损坏，请保留文件后重新配置");
  }
}
