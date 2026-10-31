import { limitedBody } from "./http-utils.ts";
import { object } from "./errors.ts";
export class Downloads {
  private value = { count: 0, url: null as string | null };
  private checked = 0;
  private inflight: Promise<void> | null = null;
  private env: NodeJS.ProcessEnv;
  private now: () => number;
  constructor(env: NodeJS.ProcessEnv, now = Date.now) {
    this.env = env;
    this.now = now;
  }
  async get() {
    if (this.now() - this.checked >= 15 * 60000 || this.checked === 0) {
      if (!this.inflight)
        this.inflight = this.refresh().finally(() => {
          this.inflight = null;
        });
      await this.inflight;
    }
    return { ...this.value };
  }
  private async refresh() {
    try {
      const base = new URL(
        this.env.GITHUB_API_BASE || "https://api.github.com",
      );
      let next: URL | null = new URL(
        "/repos/ingpoc/agent-plugins/releases?per_page=100",
        base,
      );
      const visited = new Set<string>();
      let count = 0;
      let latest = "";
      let download: string | null = null;
      while (next) {
        if (
          next.origin !== base.origin ||
          next.pathname !== "/repos/ingpoc/agent-plugins/releases" ||
          visited.has(next.href)
        )
          throw new Error("invalid_pagination");
        visited.add(next.href);
        if (visited.size > 100) throw new Error("too_many_pages");
        const response = await fetch(next, {
          signal: AbortSignal.timeout(8000),
          redirect: "error",
          headers: {
            accept: "application/vnd.github+json",
            ...(this.env.GITHUB_TOKEN
              ? { authorization: `Bearer ${this.env.GITHUB_TOKEN}` }
              : {}),
          },
        });
        if (!response.ok) throw new Error("github_unavailable");
        const releases: unknown = JSON.parse(await limitedBody(response));
        if (!Array.isArray(releases)) throw new Error("invalid_response");
        for (const value of releases) {
          const release = object(value);
          if (
            typeof release.tag_name !== "string" ||
            !release.tag_name.startsWith("agent-computer-use-v") ||
            !Array.isArray(release.assets)
          )
            continue;
          for (const item of release.assets) {
            const asset = object(item);
            if (
              typeof asset.name !== "string" ||
              !/^AgentComputerUse-.*\.dmg$/.test(asset.name)
            )
              continue;
            if (
              typeof asset.download_count !== "number" ||
              asset.download_count < 0 ||
              !Number.isSafeInteger(asset.download_count)
            )
              throw new Error("invalid_count");
            count += asset.download_count;
            const date = String(
              release.published_at ?? release.created_at ?? "",
            );
            if (
              !release.draft &&
              !release.prerelease &&
              date >= latest &&
              typeof asset.browser_download_url === "string"
            ) {
              const target = new URL(asset.browser_download_url);
              if (
                target.protocol === "https:" &&
                !target.username &&
                !target.password
              ) {
                latest = date;
                download = target.href;
              }
            }
          }
        }
        const match = response.headers
          .get("link")
          ?.match(/<([^>]+)>;\s*rel="next"/);
        next = match ? new URL(match[1], base) : null;
      }
      this.value = { count, url: download };
    } catch {
      /* Preserve the last successful count and redirect target. */
    }
    this.checked = this.now();
  }
}
