import type { Tab } from "nodriver-ts";
import { PLATFORM } from "../config.ts";
import { AuthenticationError, sleep } from "../utils.ts";
import type { Authentication, Lease } from "./auth.ts";

export class PlatformHttp {
  readonly tab: Tab;
  readonly auth: Authentication;
  readonly lease: Lease;
  constructor(tab: Tab, auth: Authentication, lease: Lease) {
    this.tab = tab;
    this.auth = auth;
    this.lease = lease;
  }
  async json(
    path: string,
    options: {
      method?: "GET" | "POST";
      body?: unknown;
      readOnly?: boolean;
      headers?: Record<string, string>;
    } = {},
  ): Promise<any> {
    const url = new URL(path, PLATFORM);
    if (url.origin !== PLATFORM || url.username || url.password)
      throw new Error("API 地址不在允许范围");
    const method = options.method || "GET";
    if (
      options.readOnly &&
      method === "POST" &&
      !url.pathname.endsWith("/course-section-progress")
    )
      throw new Error("该写入接口不可重试");
    const attempts = method === "GET" || options.readOnly ? 3 : 1;
    this.auth.assert(this.lease);
    // Navigation finishes before the SPA has restored its access token. Wait before
    // sending any request, including writes; missing readiness is not an HTTP 401.
    await this.tab.waitForFunction(
      () => {
        if (location.origin !== "https://kc.zhixueyun.com") return false;
        if (/(?:^|; )authorization=([^;]+)/.test(document.cookie)) return true;
        if (
          /(?:^|;\s*)authorization=([^;]+)/.test(
            localStorage.getItem("save_cookie") || "",
          )
        )
          return true;
        if (localStorage.getItem("saas-token")) return true;
        try {
          return !!JSON.parse(localStorage.getItem("token") || "{}")
            .access_token;
        } catch {
          return false;
        }
      },
      undefined,
      { timeout: 30000, signal: this.lease.signal },
    );
    for (let attempt = 0; attempt < attempts; attempt++) {
      this.auth.assert(this.lease);
      // Browser fetch provides native cookie domain/path/Secure/HttpOnly and Set-Cookie semantics.
      // Closing an owned tab on cancellation also stops browser-side async fetches.
      const r = await this.tab.evaluate(
        async ({ url, method, body, extra }) => {
          if (location.origin !== "https://kc.zhixueyun.com")
            throw new Error("平台页面未就绪");
          const cookie = document.cookie.match(
            /(?:^|; )authorization=([^;]+)/,
          )?.[1];
          let auth = cookie ? decodeURIComponent(cookie) : "";
          if (!auth)
            auth = decodeURIComponent(
              localStorage
                .getItem("save_cookie")
                ?.match(/(?:^|;\s*)authorization=([^;]+)/)?.[1] || "",
            );
          if (!auth) {
            try {
              const t = JSON.parse(
                localStorage.getItem("token") || "{}",
              ).access_token;
              if (t) auth = `Bearer__${t}`;
            } catch {}
          }
          if (!auth && localStorage.getItem("saas-token"))
            auth = `Bearer__${localStorage.getItem("saas-token")}`;
          if (!auth) return { status: 401, value: null };
          const response = await fetch(url, {
            method,
            credentials: "same-origin",
            redirect: "error",
            signal: AbortSignal.timeout(10000),
            headers: {
              Accept: "application/json",
              Version: "12.1.1",
              Authorization: auth,
              ...(body === undefined
                ? {}
                : { "Content-Type": "application/json" }),
              ...extra,
            },
            ...(body === undefined
              ? {}
              : {
                  body: typeof body === "string" ? body : JSON.stringify(body),
                }),
          });
          const value = response.ok ? await response.json() : null;
          return { status: response.status, value };
        },
        {
          url: url.href,
          method,
          body: options.body,
          extra: options.headers || {},
        },
        { timeout: 15000, signal: this.lease.signal },
      );
      this.auth.assert(this.lease);
      if (r.status === 401 || r.status === 403) {
        this.auth.invalidate();
        throw new AuthenticationError(
          "平台登录状态失效或访问被拒绝，请重新登录",
        );
      }
      if ([429, 502, 503, 504].includes(r.status) && attempt + 1 < attempts) {
        await sleep(250 * 2 ** attempt, this.lease.signal);
        continue;
      }
      if (r.status < 200 || r.status >= 300)
        throw new Error(`平台接口响应 ${r.status}`);
      if (!r.value || typeof r.value !== "object")
        throw new Error("平台接口未返回有效对象");
      return r.value;
    }
    throw new Error("平台接口请求未完成");
  }
}
