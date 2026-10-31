import { test } from "node:test";
import assert from "node:assert/strict";
import { startMockServices } from "../src/mock-services.ts";
import { Downloads } from "../src/downloads.ts";
import { harness } from "./helpers.ts";

function release(tag: string, count: number, date = "2026-10-31T00:00:00Z") {
  return {
    tag_name: tag,
    draft: false,
    prerelease: false,
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
    ],
  };
}

test("GitHub sums ACU DMGs and caches fifteen minutes", async (t) => {
  const mock = await startMockServices();
  t.after(mock.close);
  mock.state.githubReleases = [
    release("agent-computer-use-v1", 10),
    release("agent-computer-use-v2", 20, "2026-11-01T00:00:00Z"),
  ];
  let time = 100000;
  const downloads = new Downloads(mock.env, () => time);
  const first = await downloads.get();
  assert.equal(first.count, 30);
  assert.match(first.url!, /agent-computer-use-v2/);
  await downloads.get();
  time += 15 * 60000;
  assert.deepEqual(await downloads.get(), first);
});

test("download redirect increments redirect hits", async (t) => {
  const h = await harness(t);
  h.mock.state.githubReleases = [release("agent-computer-use-v1", 12)];
  const redirect = await h.request("/v1/public/download");
  assert.equal(redirect.status, 302);
  const stats = await h.request("/v1/public/site-stats");
  assert.equal(stats.data.downloads.redirect_hits, 1);
  assert.equal(stats.data.downloads.total, 12 + 1);
});
