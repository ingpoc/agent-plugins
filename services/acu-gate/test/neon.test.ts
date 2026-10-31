import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { NeonStore, databaseUrl } from "../src/store.ts";
import { migrate } from "../src/migrate.ts";
import { harness, signature } from "./helpers.ts";
const enabled = process.env.ACU_GATE_NEON_TEST === "1";
let store: NeonStore;
before(async () => {
  if (!enabled) return;
  if (!process.env.ACU_GATE_DATABASE_URL_FILE)
    throw new Error("ACU_GATE_DATABASE_URL_FILE is required for test:neon");
  try {
    const url = await databaseUrl(process.env);
    if (!url) throw new Error("missing");
    const schema = "acu_test_" + randomBytes(8).toString("hex");
    store = new NeonStore(url, schema);
    await migrate(process.env, schema);
    await store.migrate();
  } catch {
    throw new Error("Neon isolated-schema setup failed");
  }
});
after(async () => {
  if (store) await store.cleanupOwnSchema();
});
const options = { skip: !enabled };
test(
  "Neon: X flow, signature refresh/replay, wall consent and stats persist",
  options,
  async (t) => {
    const h = await harness(t, store),
      a = await h.login("x", "neon_x");
    assert.equal(a.status, 200);
    const unlocked = await h.request("/v1/verify/x", {
      session: a.data.session,
      data: { show_on_wall: true },
    });
    assert.equal(unlocked.status, 200);
    assert.equal(
      (await h.store.list("x_profiles")).some(
        (p) => Number(p.followers_count) === 123,
      ),
      true,
    );
    const signed = signature(a.device, "refresh");
    assert.equal(
      (await h.request("/v1/entitlement/refresh", { data: signed })).status,
      200,
    );
    assert.equal(
      (await h.request("/v1/entitlement/refresh", { data: signed })).data.error,
      "replay",
    );
    const wall = await h.request("/v1/public/site-stats");
    assert.equal(
      wall.data.wall.some((p: { handle: string }) => p.handle === "neon_x"),
      true,
    );
    await h.request("/v1/wall", {
      session: a.data.session,
      data: { show: false },
    });
    assert.equal(
      (await h.request("/v1/public/site-stats")).data.wall.some(
        (p: { handle: string }) => p.handle === "neon_x",
      ),
      false,
    );
    await h.request("/v1/public/ref-click?ref=acu-x");
    const stats = await h.request("/v1/admin/stats", { admin: true });
    assert.equal(stats.data.ref_clicks["acu-x"], 1);
    assert.equal(stats.data.unlocks.x, 1);
  },
);
test(
  "Neon: LinkedIn pending approval, status binding and reused URL rejection",
  options,
  async (t) => {
    const h = await harness(t, store),
      a = await h.login("linkedin", "neon_li");
    h.mock.pages.set("/posts/neon", { status: 999 });
    const pending = await h.request("/v1/verify/linkedin", {
      session: a.data.session,
      data: { post_url: "https://www.linkedin.com/posts/neon" },
    });
    assert.equal(pending.status, 202);
    const claim = (
      await store.list("pending_claims", { account_id: a.data.account.id })
    )[0];
    assert.equal(
      (
        await h.request(`/v1/admin/pending/${claim.id}/approve`, {
          admin: true,
          data: {},
        })
      ).status,
      200,
    );
    const status = await h.request("/v1/status", { session: a.data.session });
    assert.equal(status.status, 200);
    assert.equal(typeof status.data.entitlement, "string");
    const b = await h.login("linkedin", "other_neon_li");
    assert.equal(
      (
        await h.request("/v1/verify/linkedin", {
          session: b.data.session,
          data: { post_url: "https://www.linkedin.com/posts/neon?track=1" },
        })
      ).data.error,
      "post_url_used",
    );
  },
);
test(
  "Neon: account/device constraints, rebind cooldown and old device rejection",
  options,
  async (t) => {
    const h = await harness(t, store),
      a = await h.login("x", "neon_device");
    assert.equal(
      (await h.request("/v1/verify/x", { session: a.data.session, data: {} }))
        .status,
      200,
    );
    const taken = await h.login("x", "neon_taken", a.device);
    assert.equal(taken.data.error, "device_taken");
    const b = await h.login("x", "neon_device");
    assert.equal(
      (await h.request("/v1/status", { session: b.data.session })).data.error,
      "device_conflict",
    );
    assert.equal(
      (
        await h.request("/v1/device/rebind", {
          session: b.data.session,
          data: signature(b.device, "rebind"),
        })
      ).status,
      200,
    );
    assert.equal(
      (
        await h.request("/v1/entitlement/refresh", {
          data: signature(a.device, "refresh"),
        })
      ).data.error,
      "device_moved",
    );
    const c = await h.login("x", "neon_device");
    assert.equal(
      (
        await h.request("/v1/device/rebind", {
          session: c.data.session,
          data: signature(c.device, "rebind"),
        })
      ).data.error,
      "rebind_cooldown",
    );
  },
);
test(
  "Neon: provider linking preserves one account and one unlock",
  options,
  async (t) => {
    const h = await harness(t, store),
      a = await h.login("x", "neon_link");
    await h.request("/v1/verify/x", { session: a.data.session, data: {} });
    const b = await h.login("linkedin", "neon_link", a.device, a.data.session);
    assert.equal(b.status, 200);
    assert.equal(b.data.account.id, a.data.account.id);
    assert.equal(
      (await store.list("identities", { account_id: a.data.account.id }))
        .length,
      2,
    );
    assert.equal(
      (await store.list("unlocks", { account_id: a.data.account.id })).length,
      1,
    );
  },
);
test(
  "Neon: guarded transactions roll back a losing concurrent code redemption",
  options,
  async (t) => {
    const h = await harness(t, store),
      prepared = await h.authorize("x", "neon_race");
    const data = {
      code: prepared.code,
      app_verifier: prepared.verifier,
      device_id: prepared.device.id,
      device_pubkey: prepared.device.key,
    };
    const results = await Promise.all([
      h.request("/v1/session/redeem", { data }),
      h.request("/v1/session/redeem", { data }),
    ]);
    assert.equal(results.filter((r) => r.status === 200).length, 1);
    const lost = results.find((r) => r.status !== 200)!;
    assert.ok([401, 409].includes(lost.status));
    assert.equal(
      (await store.list("sessions", { device_id: prepared.device.id })).length,
      1,
    );
  },
);
test(
  "Neon: DMG redirect hits and default wall privacy persist",
  options,
  async (t) => {
    const h = await harness(t, store);
    h.mock.state.githubPages = [
      [
        {
          tag_name: "agent-computer-use-v-test",
          published_at: "2026-10-31T00:00:00Z",
          assets: [
            {
              name: "AgentComputerUse-arm64.dmg",
              download_count: 8,
              browser_download_url:
                "https://github.com/ingpoc/agent-plugins/releases/download/test/AgentComputerUse-arm64.dmg",
            },
          ],
        },
      ],
    ];
    assert.equal((await h.request("/v1/public/download")).status, 302);
    const stats = await h.request("/v1/public/site-stats");
    assert.deepEqual(stats.data.downloads, {
      github_dmg: 8,
      redirect_hits: 1,
      total: 9,
    });
    assert.equal(stats.data.wall.length, 0);
    assert.equal((await h.request("/v1/public/counter")).data.unlocks, 4);
  },
);
