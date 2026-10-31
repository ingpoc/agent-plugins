import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { NeonStore, databaseUrl } from "../src/store.ts";
import { migrate } from "../src/migrate.ts";
import { harness, goodPostText, xPostUrl, device, assertSameSecondReplay } from "./helpers.ts";
import { seedGoodPost } from "../src/mock-services.ts";

const enabled = process.env.ACU_GATE_NEON_TEST === "1";
let store: NeonStore;
before(async () => {
  if (!enabled) return;
  if (!process.env.ACU_GATE_DATABASE_URL_FILE)
    throw new Error("ACU_GATE_DATABASE_URL_FILE is required for test:neon");
  const url = await databaseUrl(process.env);
  if (!url) throw new Error("missing");
  const schema = "acu_test_" + randomBytes(8).toString("hex");
  store = new NeonStore(url, schema);
  await migrate(process.env, schema);
  await store.migrate();
});
after(async () => {
  if (store) await store.cleanupOwnSchema();
});
const options = { skip: !enabled };

function freshStatusId() {
  return ((BigInt(Date.now() - 1288834974657) << 22n) + 1n).toString();
}

test(
  "Neon: claim, followers push, poll unlock, refresh replay, wall",
  options,
  async (t) => {
    const h = await harness(t, store);
    const handle = "neon_x";
    const postUrl = xPostUrl(handle, freshStatusId());
    seedGoodPost(h.mock.state, postUrl, handle, goodPostText());
    const d = device();
    const created = await h.claim("x", handle, d, postUrl);
    assert.equal(created.status, 201);
    await h.request("/v1/admin/followers", {
      admin: true,
      data: { provider: "x", followers: [{ id: "1", handle }], source: "x_plugin" },
    });
    const polled = await h.poll(String(created.data.claim_id), created.device);
    assert.equal(polled.data.status, "unlocked");
    const { signRequest } = await import("./helpers.ts");
    const signed = signRequest(created.device, "refresh", {
      device_id: created.device.id,
    });
    assert.equal(
      (
        await fetch(`${h.base}/v1/entitlement/refresh`, {
          method: "POST",
          headers: signed.headers,
          body: signed.body,
        })
      ).status,
      200,
    );
    const replay = await fetch(`${h.base}/v1/entitlement/refresh`, {
      method: "POST",
      headers: signed.headers,
      body: signed.body,
    });
    assert.equal((await replay.json()).error, "bad_signature");
  },
);

test("Neon: LinkedIn admin verify unlock persists", options, async (t) => {
  const h = await harness(t, store);
  const slug = "neon_li";
  const postUrl = "https://www.linkedin.com/posts/neon";
  const created = await h.claim(
    "linkedin",
    `https://www.linkedin.com/in/${slug}`,
    device(),
    postUrl,
  );
  assert.equal(created.data.status, "pending_verification");
  await h.request(`/v1/admin/claims/${created.data.claim_id}/verify`, {
    admin: true,
    data: {
      post_url: postUrl,
      post_text: goodPostText().replace("acu-x", "acu-li"),
      source: "comet",
    },
  });
  const polled = await h.poll(String(created.data.claim_id), created.device);
  assert.equal(polled.data.status, "unlocked");
});

test("Neon: same-second claim and poll succeed; replay is rejected", options, async (t) => {
  await assertSameSecondReplay(await harness(t, store));
});
