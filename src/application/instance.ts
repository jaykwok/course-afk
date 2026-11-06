import { mkdir, readFile, rm, realpath } from "node:fs/promises";
import { createServer } from "node:net";
import { join } from "node:path";
import { atomicJson } from "../storage/files.ts";
import { token, digest } from "../utils.ts";
export interface Instance {
  pid: number;
  id: string;
  origin?: string;
  key?: string;
}
export async function acquireInstance(directory: string): Promise<
  | {
      owned: true;
      publish: (origin: string, key: string) => Promise<void>;
      close: () => Promise<void>;
    }
  | { owned: false; instance: Instance }
> {
  await mkdir(directory, { recursive: true });
  const resolved = await realpath(directory),
    key = process.platform === "win32" ? resolved.toLowerCase() : resolved;
  // The OS releases this mutually exclusive socket even after a hard host exit.
  // Metadata only reopens the winning instance; it is never the lock itself.
  const port = 40000 + (parseInt(digest(key).slice(0, 8), 16) % 20000),
    path = join(directory, "instance.json"),
    id = token();
  const guard = createServer((socket) => socket.destroy());
  try {
    await new Promise<void>((resolve, reject) => {
      guard.once("error", reject);
      guard.listen({ host: "127.0.0.1", port, exclusive: true }, () =>
        resolve(),
      );
    });
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "EADDRINUSE") throw e;
    let instance: Instance;
    try {
      instance = JSON.parse(await readFile(path, "utf8"));
    } catch {
      throw new Error("已有工作台正在启动，或实例端口被占用，请稍后重试");
    }
    return { owned: false, instance };
  }
  try {
    await atomicJson(path, { pid: process.pid, id });
  } catch (e) {
    guard.close();
    throw e;
  }
  return {
    owned: true,
    publish: async (origin, key) => {
      await atomicJson(path, { pid: process.pid, id, origin, key });
    },
    close: async () => {
      try {
        const value = JSON.parse(await readFile(path, "utf8"));
        if (value.id === id) await rm(path);
      } finally {
        await new Promise<void>((resolve, reject) =>
          guard.close((e) => (e ? reject(e) : resolve())),
        );
      }
    },
  };
}
export async function reopenInstance(instance: Instance) {
  if (!instance.origin || !instance.key)
    throw new Error("工作台正在启动，请稍后重试");
  const url = new URL(instance.origin);
  if (
    url.protocol !== "http:" ||
    url.hostname !== "127.0.0.1" ||
    !url.port ||
    url.username ||
    url.password
  )
    throw new Error("工作台地址无效");
  const r = await fetch(`${url.origin}/api/reopen`, {
    method: "POST",
    redirect: "error",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key: instance.key }),
    signal: AbortSignal.timeout(5000),
  });
  if (!r.ok) throw new Error("无法重开已有工作台");
}
