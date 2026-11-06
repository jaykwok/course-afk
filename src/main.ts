import { configuration } from "./config.ts";
import { Logger, redact } from "./log.ts";
import { Repository } from "./storage/repository.ts";
import { BrowserRuntime, executable } from "./browser/runtime.ts";
import { Application } from "./application/runtime.ts";
import { WebServer } from "./web/server.ts";
import { acquireInstance, reopenInstance } from "./application/instance.ts";

async function main() {
  const version = process.versions.node.split(".").map(Number);
  if (version[0] < 24 || (version[0] === 24 && version[1] < 22))
    throw new Error("请安装 Node.js 24.22.0 或更高版本");
  const config = configuration();
  if (process.argv.includes("--check-startup")) {
    executable(config.browserPath);
    console.log("Node.js 和浏览器已就绪");
    return;
  }
  const lock = await acquireInstance(config.dataDir);
  if (!lock.owned) {
    await reopenInstance(lock.instance);
    return;
  }
  let repo: Repository | undefined,
    browser: BrowserRuntime | undefined,
    server: WebServer | undefined,
    app: Application | undefined;
  try {
    const logger = new Logger(config.logsDir);
    repo = new Repository(config.dataDir);
    browser = await BrowserRuntime.launch(logger, config.browserPath);
    app = new Application(repo, browser, config, logger);
    server = new WebServer(app);
    const url = await server.start();
    server.reopen = async () => {
      if (browser!.management && !browser!.management.closed)
        await browser!.management.bringToFront();
      else throw new Error("管理窗口已关闭，服务正在退出");
    };
    await lock.publish(server.origin, server.reopenKey);
    await browser.openManagement(url);
    // A closed management page is the lifecycle boundary; business popups never own shutdown.
    const off = browser.browser.cdp.on(
      "Target.targetDestroyed",
      ({ targetId }) => {
        if (targetId === browser!.management?.id) app!.exit();
      },
    );
    const signal = () => app!.exit();
    process.on("SIGINT", signal);
    process.on("SIGTERM", signal);
    try {
      await new Promise<void>((resolve) => app!.once("exit", resolve));
      await app.settled();
    } finally {
      off();
      process.off("SIGINT", signal);
      process.off("SIGTERM", signal);
    }
  } finally {
    try {
      await server?.close();
    } finally {
      try {
        await browser?.close();
      } finally {
        repo?.close();
        await lock.close();
      }
    }
  }
}
main().catch((e) => {
  console.error(redact(e));
  process.exitCode = 1;
});
