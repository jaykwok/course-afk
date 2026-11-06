import { configuration } from "../src/config.ts";
import { Repository } from "../src/storage/repository.ts";
import { acquireInstance } from "../src/application/instance.ts";
import { atomicJson } from "../src/storage/files.ts";
import { resolve } from "node:path";
const config = configuration(),
  lock = await acquireInstance(config.dataDir);
if (!lock.owned) throw new Error("请先关闭工作台，再使用数据工具");
let repo: Repository | undefined;
try {
  repo = new Repository(config.dataDir);
  const data = repo.export();
  const index = process.argv.indexOf("--output");
  if (index >= 0) {
    if (!process.argv[index + 1]) throw new Error("--output 缺少路径");
    await atomicJson(resolve(process.argv[index + 1]), data);
    console.log("已导出工作台数据");
  } else
    console.log(
      JSON.stringify(
        Object.fromEntries(
          Object.entries(data).map(([key, rows]) => [key, rows.length]),
        ),
        null,
        2,
      ),
    );
} finally {
  repo?.close();
  await lock.close();
}
