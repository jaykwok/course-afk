import {
  appendFileSync,
  mkdirSync,
  existsSync,
  statSync,
  renameSync,
} from "node:fs";
import { join } from "node:path";
export function redact(input: unknown): string {
  return String(input instanceof Error ? input.message : input)
    .replace(
      /([?&#](?:token|code|ticket|authorization|bootstrap|key|access_token)=)[^&#\s]*/gi,
      "$1[已隐藏]",
    )
    .replace(
      /((?:authorization|cookie|x-token|api[_-]?key|access[_-]?token|token|ticket|bootstrap|password)\s*[:=]\s*)[^\r\n,;]+/gi,
      "$1[已隐藏]",
    )
    .replace(/\bBearer(?:__|\s+)[\w.\-]+/gi, "Bearer [已隐藏]")
    .replace(/\bsk-[\w-]+/g, "[已隐藏]")
    .slice(0, 12000);
}
export class Logger {
  readonly directory: string;
  listener?: (message: string, level: string) => void;
  constructor(directory: string) {
    this.directory = directory;
    mkdirSync(directory, { recursive: true });
  }
  log(message: unknown, level: "info" | "warn" | "error" = "info") {
    const text = redact(message),
      path = join(this.directory, `app-${level}.log`);
    const today = new Date().toISOString().slice(0, 10);
    if (existsSync(path)) {
      const day = statSync(path).mtime.toISOString().slice(0, 10);
      if (day !== today)
        renameSync(
          path,
          join(this.directory, `app-${level}-${day}-${Date.now()}.log`),
        );
    }
    appendFileSync(path, `${new Date().toISOString()} [${level}] ${text}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    this.listener?.(text, level);
    if (process.env.COURSE_AFK_QUIET !== "1") console.log(text);
  }
}
