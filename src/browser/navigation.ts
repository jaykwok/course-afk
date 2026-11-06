import { NodriverError, type Tab } from "nodriver-ts";

/** SSO may replace the first document before its DOMContentLoaded event. */
export async function navigateDocument(
  tab: Tab,
  url: string,
  signal: AbortSignal,
  timeout = 30000,
) {
  signal.throwIfAborted();
  const abort = new AbortController();
  const bounded = AbortSignal.any([
    signal,
    abort.signal,
    AbortSignal.timeout(timeout),
  ]);
  const deadline = Date.now() + timeout;
  const options = () => ({
    signal: bounded,
    timeout: Math.max(1, deadline - Date.now()),
  });
  // Arm before the command. Follow a main-frame commit without pinning the
  // lifecycle wait to Page.navigate's initial loader (which SSO can replace).
  const committed = tab.waitForNavigation({
    ...options(),
    waitUntil: "commit",
  });
  try {
    await Promise.all([
      committed,
      (async () => {
        const result = await tab.cdp.sendWith("Page.navigate", options(), {
          url,
        });
        if (result.errorText)
          throw new NodriverError(`Navigation failed: ${result.errorText}`);
        if (result.isDownload)
          throw new NodriverError("页面导航变成了文件下载");
      })(),
    ]);
    // nodriver's wait retries only replaced execution contexts; disconnects,
    // crashes and other errors still propagate. Authentication is checked later.
    await tab.waitForFunction(
      () => document.readyState !== "loading",
      undefined,
      { ...options(), world: "isolated" },
    );
  } finally {
    abort.abort();
  }
}
