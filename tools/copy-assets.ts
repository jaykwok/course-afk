import { cp, mkdir } from "node:fs/promises";
await mkdir("dist/src/web/static", { recursive: true });
await cp("src/web/static", "dist/src/web/static", { recursive: true });
