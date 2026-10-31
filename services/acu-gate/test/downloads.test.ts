import { test } from "node:test";
import assert from "node:assert/strict";
import { startMocks } from "../src/mock-providers.ts";
import { Downloads } from "../src/downloads.ts";
import { harness } from "./helpers.ts";
function release(tag: string, count: number, date = "2026-10-31T00:00:00Z") {
  return {
    tag_name: tag,
    published_at: date,
    assets: [
      {
        name: "AgentComputerUse-arm64.dmg",
        download_count: count,
        browser_download_url:
          "https://github.com/ingpoc/agent-plugins/releases/download/" +
          tag +
          "/AgentComputerUse-arm64.dmg",
      },
      { name: "checksums.txt", download_count: 999 },
    ],
  };
}
test("GitHub paginates, sums only ACU DMGs, caches fifteen minutes and retains stale values", async (t) => {
  const mock = await startMocks();
  t.after(mock.close);
  mock.state.githubPages = [
    [release("agent-computer-use-v1", 10), release("comet-control-v1", 500)],
    [release("agent-computer-use-v2", 20, "2026-11-01T00:00:00Z")],
  ];
  let time = 100000;
  const downloads = new Downloads(mock.env, () => time);
  const first = await downloads.get();
  assert.equal(first.count, 30);
  assert.match(first.url!, /agent-computer-use-v2/);
  assert.equal(mock.state.githubCalls, 2);
  await downloads.get();
  assert.equal(mock.state.githubCalls, 2);
  time += 15 * 60000;
  mock.state.githubStatus = 500;
  assert.deepEqual(await downloads.get(), first);
  assert.equal(mock.state.githubCalls, 3);
  time += 15 * 60000;
  mock.state.githubStatus = 200;
  mock.state.githubPages = [[release("agent-computer-use-v3", 40)]];
  assert.equal((await downloads.get()).count, 40);
});
test("download redirects count daily hits and site/admin stats expose totals", async (t) => {
  const h = await harness(t);
  h.mock.state.githubPages = [[release("agent-computer-use-v1", 12)]];
  const redirect = await h.request("/v1/public/download");
  assert.equal(redirect.status, 302);
  assert.match(
    redirect.headers.get("location")!,
    /AgentComputerUse-arm64\.dmg$/,
  );
  const stats = await h.request("/v1/public/site-stats");
  assert.deepEqual(stats.data.downloads, {
    github_dmg: 12,
    redirect_hits: 1,
    total: 13,
  });
  assert.equal(stats.headers.get("cache-control"), "public, max-age=60");
  assert.equal((await h.store.list("download_hits")).length, 1);
  const admin = await h.request("/v1/admin/stats", { admin: true });
  assert.deepEqual(admin.data.downloads, stats.data.downloads);
});
