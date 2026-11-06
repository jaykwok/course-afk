import type { Tab } from "nodriver-ts";
import type { Protocol } from "devtools-protocol";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { HOME, CENTER, PLATFORM } from "../config.ts";
import { accountId, entry, platformHost } from "../domain/identity.ts";
import { atomicJson } from "../storage/files.ts";
import { AuthenticationError, digest, until } from "../utils.ts";
import type { BrowserRuntime, TabScope } from "./runtime.ts";
import { navigateDocument } from "./navigation.ts";
import { waitForLoginReturn } from "./login.ts";

interface Credential {
  version: 2;
  account: string;
  name: string;
  expires: string;
  cookies: Protocol.Network.CookieParam[];
}
export interface Lease {
  account: string;
  generation: number;
  fingerprint: string;
  signal: AbortSignal;
}
export class Authentication {
  readonly runtime: BrowserRuntime;
  readonly path: string;
  generation = 0;
  private current?: Credential;
  private fingerprint = "";
  private verified = false;
  constructor(runtime: BrowserRuntime, directory: string) {
    this.runtime = runtime;
    this.path = join(directory, "credentials.json");
  }
  read(): Credential | undefined {
    if (!existsSync(this.path)) return undefined;
    try {
      const c = JSON.parse(readFileSync(this.path, "utf8"));
      if (
        c.version !== 2 ||
        typeof c.account !== "string" ||
        !c.account ||
        typeof c.name !== "string" ||
        typeof c.expires !== "string" ||
        !Array.isArray(c.cookies) ||
        !c.cookies.length ||
        c.cookies.some(
          (cookie: any) =>
            !cookie ||
            typeof cookie.name !== "string" ||
            typeof cookie.value !== "string" ||
            (typeof cookie.domain !== "string" &&
              typeof cookie.url !== "string"),
        )
      )
        return undefined;
      return c;
    } catch {
      return undefined;
    }
  }
  fileFingerprint() {
    return existsSync(this.path) ? digest(readFileSync(this.path, "utf8")) : "";
  }
  dashboard() {
    const c = this.read(),
      expires = c ? Date.parse(c.expires) : 0;
    return {
      account: c?.name || c?.account || "",
      credential: {
        status: c ? (expires > Date.now() ? "valid" : "expired") : "missing",
        expires_on: c?.expires.slice(0, 10),
        days_left: Math.max(0, Math.ceil((expires - Date.now()) / 86400000)),
      },
    };
  }
  async clear() {
    this.generation++;
    this.verified = false;
    this.current = undefined;
    this.fingerprint = "";
    await this.runtime.clearPlatform();
  }
  invalidate() {
    this.generation++;
    this.verified = false;
  }
  async forget() {
    await this.clear();
    await atomicJson(this.path, { version: 2, cookies: [] });
  }
  async profile(
    scope: TabScope,
    timeout = 30000,
    existing?: Tab,
  ): Promise<{ account: string; name: string }> {
    const tab = existing || (await scope.open());
    try {
      await navigateDocument(tab, `${CENTER}/PC/home`, scope.signal, timeout);
      await tab.waitForFunction(
        () =>
          location.origin === "https://center.mylearning.cn" &&
          !!localStorage.getItem("token"),
        undefined,
        { timeout, signal: scope.signal },
      );
      const result = await tab.evaluate(
        async () => {
          if (location.origin !== "https://center.mylearning.cn")
            throw new Error("认证页面变化");
          const t = localStorage.getItem("token");
          if (!t) throw new Error("未登录");
          const r = await fetch("/api/personal/user/info", {
            headers: { "x-token": t },
            redirect: "error",
            signal: AbortSignal.timeout(15000),
          });
          if (!r.ok) throw new Error("账号核验失败");
          const j = await r.json();
          if (
            j.code !== 0 ||
            !["string", "number"].includes(typeof j.data?.id) ||
            !String(j.data.id).trim()
          )
            throw new Error("账号标识缺失");
          return {
            account: `center:${j.data.id}`,
            name: String(j.data.name || ""),
          };
        },
        undefined,
        { timeout: 20000, signal: scope.signal },
      );
      return result;
    } finally {
      if (!existing) await tab.close();
    }
  }
  async login(scope: TabScope, report: (text: string) => void) {
    await this.clear();
    const tab = await scope.open(HOME);
    await tab.bringToFront();
    report("请在浏览器平台页完成登录，登录成功后将自动核验账号");
    await waitForLoginReturn(tab, scope.signal);
    report("登录已返回平台，正在核验账号");
    const profile = await this.profile(scope, 30000, tab);
    const cookies = (await this.runtime.context.cookies())
      .filter((c) => platformHost(c.domain.replace(/^\./, "")))
      .map(cookieParam);
    if (!cookies.length) throw new AuthenticationError("未获得有效登录凭证");
    const c: Credential = {
      version: 2,
      ...profile,
      expires: new Date(Date.now() + 28 * 86400000).toISOString(),
      cookies,
    };
    scope.signal.throwIfAborted();
    await atomicJson(this.path, c);
    this.current = c;
    this.fingerprint = this.fileFingerprint();
    this.verified = true;
    await tab.close();
    report(`已登录 ${profile.name || profile.account}`);
  }
  async lease(scope: TabScope): Promise<Lease> {
    const c = this.read(),
      fingerprint = this.fileFingerprint();
    if (
      !c ||
      !Number.isFinite(Date.parse(c.expires)) ||
      Date.parse(c.expires) <= Date.now()
    )
      throw new AuthenticationError("请先登录或更新凭证");
    if (!this.verified || fingerprint !== this.fingerprint) {
      await this.clear();
      await this.runtime.context.setCookies(
        c.cookies.filter((c) =>
          platformHost(
            (c.domain || new URL(c.url || PLATFORM).hostname).replace(
              /^\./,
              "",
            ),
          ),
        ),
      );
      const home = await scope.open(HOME);
      let profile;
      try {
        profile = await this.profile(scope, 30000, home);
      } finally {
        await home.close();
      }
      if (profile.account !== c.account)
        throw new AuthenticationError("浏览器账号与凭证不一致，请重新登录");
      if (this.fileFingerprint() !== fingerprint)
        throw new AuthenticationError("凭证在核验过程中发生变化，请重试");
      this.current = c;
      this.fingerprint = fingerprint;
      this.verified = true;
    }
    return {
      account: accountId(c.account),
      generation: this.generation,
      fingerprint,
      signal: scope.signal,
    };
  }
  assert(lease: Lease) {
    lease.signal.throwIfAborted();
    if (
      !this.verified ||
      !this.current ||
      lease.generation !== this.generation ||
      lease.fingerprint !== this.fileFingerprint() ||
      lease.account !== accountId(this.current.account)
    )
      throw new AuthenticationError("凭证或账号已变化，本轮操作已停止");
  }
  async navigate(scope: TabScope, url: string, lease: Lease): Promise<Tab> {
    this.assert(lease);
    const wanted = entry(url, lease.account);
    const tab = await scope.open(wanted.url);
    let reopened = false;
    await until(
      async () => {
        this.assert(lease);
        if (tab.closed) throw new Error("任务页面已关闭");
        const actual = (() => {
          try {
            return entry(tab.url, lease.account);
          } catch {
            return undefined;
          }
        })();
        if (actual && (wanted.kind === "entry" || actual.id === wanted.id)) {
          const ready = await tab.evaluate(
            () => {
              const login = [
                ...document.querySelectorAll(
                  "input[type=password],#esurfingloginiframe",
                ),
              ].some((e) => e.getBoundingClientRect().height > 0);
              return (
                !login &&
                !/(?:\/login|\/sso|\/oauth|\/authorize)(?:[/?#]|$)/i.test(
                  location.pathname + location.hash.split("?")[0],
                )
              );
            },
            undefined,
            { timeout: 5000, signal: lease.signal },
          );
          if (ready) return true;
        }
        if (
          !reopened &&
          actual?.kind === "entry" &&
          new URL(tab.url).hostname === "kc.zhixueyun.com" &&
          wanted.kind !== "entry"
        ) {
          const profile = await this.profile(scope);
          if (accountId(profile.account) !== lease.account)
            throw new AuthenticationError("认证回跳账号不一致");
          this.assert(lease);
          await navigateDocument(tab, url, lease.signal);
          reopened = true;
        }
        return false;
      },
      45000,
      lease.signal,
      500,
    );
    return tab;
  }
}
export function cookieParam(
  c: Protocol.Network.Cookie,
): Protocol.Network.CookieParam {
  return {
    name: c.name,
    value: c.value,
    domain: c.domain,
    path: c.path,
    secure: c.secure,
    httpOnly: c.httpOnly,
    sameSite: c.sameSite,
    ...(c.expires > 0 ? { expires: c.expires } : {}),
    ...(c.partitionKey ? { partitionKey: c.partitionKey } : {}),
  };
}
