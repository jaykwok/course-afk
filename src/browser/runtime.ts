import { Browser, type BrowserContext, type Tab } from "nodriver-ts";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import {
  createProfile,
  validateProfile,
  removeProfile,
  prepareProjectProfile,
} from "./profile.ts";
import type { Logger } from "../log.ts";
import { navigateDocument } from "./navigation.ts";
const loopback = (host: string) =>
  ["127.0.0.1", "localhost", "::1", "[::1]"].includes(host.replace(/^\./, ""));

export function executable(override?: string): string {
  const candidates = override
    ? [override]
    : process.platform === "win32"
      ? [
          join(
            process.env["ProgramFiles(x86)"] || "C:\\Program Files (x86)",
            "Microsoft/Edge/Application/msedge.exe",
          ),
          join(
            process.env.ProgramFiles || "C:\\Program Files",
            "Microsoft/Edge/Application/msedge.exe",
          ),
          join(
            process.env.LOCALAPPDATA || "",
            "Microsoft/Edge/Application/msedge.exe",
          ),
          join(
            process.env.ProgramFiles || "C:\\Program Files",
            "Google/Chrome/Application/chrome.exe",
          ),
          join(
            process.env["ProgramFiles(x86)"] || "C:\\Program Files (x86)",
            "Google/Chrome/Application/chrome.exe",
          ),
          join(
            process.env.LOCALAPPDATA || "",
            "Google/Chrome/Application/chrome.exe",
          ),
        ]
      : process.platform === "darwin"
        ? [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            join(
              homedir(),
              "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ),
          ]
        : [
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
          ];
  const found = candidates.find((p) => existsSync(p));
  if (!found)
    throw new Error(
      override
        ? "设置中的浏览器路径不存在，请检查 data/settings.json 的 browser.executable"
        : "未找到浏览器，请安装 Microsoft Edge 或 Google Chrome",
    );
  return found;
}
export class BrowserRuntime {
  readonly browser: Browser;
  readonly context: BrowserContext;
  readonly profile?: string;
  private readonly temporaryProfile: boolean;
  private startupTab?: Tab;
  management?: Tab;
  readonly retained = new Set<Tab>();
  private origins = new Set([
    "https://www.mylearning.cn",
    "https://center.mylearning.cn",
    "https://kc.zhixueyun.com",
    "https://cms.mylearning.cn",
    "https://www.ctexpert.cn",
    "https://ctexpert.cn",
  ]);
  private unsubscribe: () => void;
  private constructor(
    browser: Browser,
    logger: Logger,
    profile?: string,
    temporaryProfile = true,
  ) {
    this.browser = browser;
    this.context = browser.defaultContext;
    this.profile = profile;
    this.temporaryProfile = temporaryProfile;
    // Capture only the launch tab, before any business scopes create their tabs.
    this.startupTab = this.context.tabs.find(
      (tab) =>
        !tab.closed &&
        tab.url === "about:blank" &&
        !browser.targetInfo.get(tab.id)?.openerId,
    );
    this.unsubscribe = browser.cdp.on(
      "Target.targetInfoChanged",
      ({ targetInfo }) => this.note(targetInfo.url),
    );
    for (const tab of browser.tabs) tab.setInputPolicy({ mode: "human" });
    browser.tabCreated.on((tab) => tab.setInputPolicy({ mode: "human" }));
    browser.errors.on((error) => logger.log(error, "warn"));
  }
  static async launch(
    logger: Logger,
    path?: string,
    profileDirectory?: string,
  ) {
    const executablePath = executable(path),
      profile = profileDirectory
        ? await prepareProjectProfile(profileDirectory)
        : await createProfile();
    try {
      await validateProfile(profile);
      const browser = await Browser.launch({
        executablePath,
        userDataDir: profile,
        headless: false,
        detached: true,
        supervised: true,
        timeout: 30000,
        preset: "agent",
        args: [
          "--disable-background-timer-throttling",
          "--disable-extensions",
          "--disable-sync",
          "--profile-directory=Default",
          "--disable-features=LocalNetworkAccessChecks,LocalNetworkAccessChecksWebRTC,msForceBrowserSignIn,msImplicitSignin,msEdgeOnRampFRE",
        ],
        onError: (e) => logger.log(e, "warn"),
      });
      return new BrowserRuntime(browser, logger, profile, !profileDirectory);
    } catch (e) {
      if (!profileDirectory) await removeProfile(profile);
      throw e;
    }
  }
  static fromBrowser(browser: Browser, logger: Logger) {
    return new BrowserRuntime(browser, logger);
  }
  note(url: string) {
    try {
      const u = new URL(url);
      if (["http:", "https:"].includes(u.protocol) && !loopback(u.hostname))
        this.origins.add(u.origin);
    } catch {}
  }
  async openManagement(url: string) {
    const startup = this.startupTab;
    this.startupTab = undefined;
    if (startup && !startup.closed && startup.url === "about:blank") {
      await startup.goto(url);
      this.management = startup;
    } else this.management = await this.context.newTab(url);
    await this.management.bringToFront();
    return this.management;
  }
  scope(signal: AbortSignal) {
    return new TabScope(this, signal);
  }
  async clearPlatform() {
    // Close platform pages first; they must not repopulate storage during invalidation.
    for (const tab of this.context.tabs)
      if (tab !== this.management && !tab.url.startsWith("http://127.0.0.1:")) {
        this.note(tab.url);
        await tab.close();
      }
    this.retained.clear();
    const session = this.management?.cdp || this.context.tabs[0]?.cdp;
    if (!session) throw new Error("平台会话清理缺少活动页面");
    for (const cookie of await this.context.cookies()) {
      if (!loopback(cookie.domain))
        this.origins.add(`https://${cookie.domain.replace(/^\./, "")}`);
    }
    for (const origin of this.origins)
      await session.send("Storage.clearDataForOrigin", {
        origin,
        storageTypes: "all",
      });
    // Explicit cookie deletion catches domain cookies not covered by origin storage.
    for (const cookie of await this.context.cookies())
      if (!loopback(cookie.domain)) {
        await session.send("Network.deleteCookies", {
          name: cookie.name,
          domain: cookie.domain,
          path: cookie.path,
          ...(cookie.partitionKey ? { partitionKey: cookie.partitionKey } : {}),
        });
      }
  }
  async close() {
    this.unsubscribe();
    await this.browser.close();
    if (this.profile) {
      if (
        this.browser.process &&
        !(await this.browser.process.waitForExit(10000))
      )
        throw new Error("浏览器尚未退出，配置目录已保留");
      if (this.temporaryProfile) await removeProfile(this.profile);
    }
  }
}
export class TabScope {
  readonly runtime: BrowserRuntime;
  readonly signal: AbortSignal;
  readonly tabs = new Set<Tab>();
  private stop: () => void;
  private cancelled = false;
  private abortListener: () => void;
  constructor(runtime: BrowserRuntime, signal: AbortSignal) {
    this.runtime = runtime;
    this.signal = signal;
    this.stop = runtime.browser.tabCreated.on((tab) => {
      const opener = runtime.browser.targetInfo.get(tab.id)?.openerId;
      if (opener && [...this.tabs].some((t) => t.id === opener))
        this.tabs.add(tab);
    });
    this.abortListener = () => {
      this.cancelled = true;
      void this.close().catch(() => {});
    };
    signal.addEventListener("abort", this.abortListener, { once: true });
  }
  async open(url?: string, timeout = 30000) {
    this.signal.throwIfAborted();
    const tab = await this.runtime.context.newTab();
    this.tabs.add(tab);
    if (this.cancelled || this.signal.aborted) {
      await tab.close();
      this.signal.throwIfAborted();
    }
    if (url) {
      this.runtime.note(url);
      try {
        await navigateDocument(tab, url, this.signal, timeout);
      } catch (e) {
        await tab.close().catch(() => {});
        this.tabs.delete(tab);
        throw e;
      }
    }
    return tab;
  }
  retain(tab: Tab) {
    if (!this.tabs.has(tab)) throw new Error("标签不属于当前任务");
    this.runtime.retained.add(tab);
  }
  async close() {
    this.stop();
    this.signal.removeEventListener("abort", this.abortListener);
    const errors = await Promise.allSettled(
      [...this.tabs]
        .filter(
          (t) => !this.runtime.retained.has(t) && t !== this.runtime.management,
        )
        .map((t) => t.close()),
    );
    this.tabs.clear();
    const failed = errors.find((r) => r.status === "rejected");
    if (failed?.status === "rejected") throw failed.reason;
  }
}
