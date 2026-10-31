import { test } from "node:test";
import assert from "node:assert/strict";
import { createPublicKey, verify } from "node:crypto";
import {
  harness,
  device,
  signRequest,
  goodPostText,
  xPostUrl,
} from "./helpers.ts";
import { seedGoodPost } from "../src/mock-services.ts";
import { snowflakeMs } from "../src/handles.ts";

function freshStatusId(offsetMs = 0) {
  const ms = Date.now() + offsetMs;
  return ((BigInt(ms - 1288834974657) << 22n) + 1n).toString();
}

test("claim validation rejects bad handles and URLs", async (t) => {
  const h = await harness(t);
  const d = device();
  for (const handle of ["", "bad handle", "x".repeat(20)]) {
    const signed = signRequest(d, "claim", {
      provider: "x",
      handle,
      post_url: null,
      device_pubkey: d.key,
      device_name: "Mac",
    });
    const r = await fetch(`${h.base}/v1/claims`, {
      method: "POST",
      headers: signed.headers,
      body: signed.body,
    });
    assert.equal((await r.json()).error, "invalid_handle");
  }
});

test("instant oEmbed pass unlocks when follow already known", async (t) => {
  const h = await harness(t);
  const handle = "alice";
  const postUrl = xPostUrl(handle, freshStatusId());
  seedGoodPost(h.mock.state, postUrl, handle, goodPostText());
  await h.store.atomic([
    {
      kind: "insert",
      table: "followers",
      row: { provider: "x", handle, x_user_id: "1" },
    },
  ]);
  const created = await h.claim("x", handle, device(), postUrl);
  assert.equal(created.status, 201);
  assert.equal(created.data.status, "unlocked");
  assert.ok(created.data.entitlement);
});

test("oEmbed author mismatch keeps post failed", async (t) => {
  const h = await harness(t);
  const postUrl = xPostUrl("bob", freshStatusId());
  seedGoodPost(h.mock.state, postUrl, "bob", goodPostText());
  const created = await h.claim("x", "alice", device(), postUrl);
  assert.equal(created.data.checks.post, "failed");
});

test("oEmbed snowflake too old fails post check", async (t) => {
  const h = await harness(t);
  const postUrl = xPostUrl("alice", "1000");
  seedGoodPost(h.mock.state, postUrl, "alice", goodPostText());
  const created = await h.claim("x", "alice", device(), postUrl);
  assert.equal(created.data.checks.post, "failed");
});

test("oEmbed 404 leaves post pending", async (t) => {
  const h = await harness(t);
  const postUrl = xPostUrl("alice", freshStatusId());
  const created = await h.claim("x", "alice", device(), postUrl);
  assert.equal(created.data.checks.post, "pending");
});

test("used post URL rejects second account", async (t) => {
  const h = await harness(t);
  const handle = "alice";
  const postUrl = xPostUrl(handle, freshStatusId());
  seedGoodPost(h.mock.state, postUrl, handle, goodPostText());
  await h.store.atomic([
    {
      kind: "insert",
      table: "followers",
      row: { provider: "x", handle, x_user_id: "1" },
    },
  ]);
  await h.claim("x", handle, device(), postUrl);
  const second = await h.claim("x", "bob", device(), postUrl);
  assert.equal(second.data.checks.post, "failed");
});

test("followers push confirms follow and unlocks", async (t) => {
  const h = await harness(t);
  const d = device();
  const handle = "fan1";
  const postUrl = xPostUrl(handle, freshStatusId());
  seedGoodPost(h.mock.state, postUrl, handle, goodPostText());
  const created = await h.claim("x", handle, d, postUrl);
  assert.equal(created.data.checks.follow, "pending");
  const push = await h.request("/v1/admin/followers", {
    admin: true,
    data: {
      provider: "x",
      followers: [{ id: "9", handle }],
      source: "x_plugin",
    },
  });
  assert.equal(push.data.unlocked_claims.length, 1);
  const polled = await h.poll(String(created.data.claim_id), d);
  assert.equal(polled.data.status, "unlocked");
});

test("claim before followers then push unlocks", async (t) => {
  const h = await harness(t);
  const handle = "fan2";
  const postUrl = xPostUrl(handle, freshStatusId());
  seedGoodPost(h.mock.state, postUrl, handle, goodPostText());
  const created = await h.claim("x", handle, device(), postUrl);
  await h.request("/v1/admin/followers", {
    admin: true,
    data: { provider: "x", followers: [{ handle }], source: "x_plugin" },
  });
  const polled = await h.poll(String(created.data.claim_id), created.device);
  assert.equal(polled.data.status, "unlocked");
});

test("admin comet verify only when oEmbed 404", async (t) => {
  const h = await harness(t);
  const handle = "comet1";
  const postUrl = xPostUrl(handle, freshStatusId());
  const created = await h.claim("x", handle, device(), postUrl);
  await h.request("/v1/admin/followers", {
    admin: true,
    data: { provider: "x", followers: [{ handle }], source: "x_plugin" },
  });
  seedGoodPost(h.mock.state, postUrl, handle, goodPostText());
  const bad = await h.request(`/v1/admin/claims/${created.data.claim_id}/verify`, {
    admin: true,
    data: {
      post_url: postUrl,
      post_text: goodPostText(),
      source: "comet",
      follow_confirmed: true,
    },
  });
  assert.equal(bad.status, 422);
  h.mock.state.oembed.set(postUrl, { status: 404 });
  const ok = await h.request(`/v1/admin/claims/${created.data.claim_id}/verify`, {
    admin: true,
    data: {
      post_url: postUrl,
      post_text: goodPostText(),
      source: "comet",
      follow_confirmed: true,
    },
  });
  assert.equal(ok.data.status, "unlocked");
});

test("LinkedIn pending then admin verify unlocks", async (t) => {
  const h = await harness(t);
  const slug = "li-user";
  const postUrl = "https://www.linkedin.com/posts/abc";
  const created = await h.claim("linkedin", `https://www.linkedin.com/in/${slug}`, device(), postUrl);
  assert.equal(created.data.status, "pending_verification");
  const ok = await h.request(`/v1/admin/claims/${created.data.claim_id}/verify`, {
    admin: true,
    data: {
      post_url: postUrl,
      post_text: goodPostText().replace("acu-x", "acu-li"),
      source: "comet",
    },
  });
  assert.equal(ok.data.status, "unlocked");
});

test("LinkedIn auto-pass on public page", async (t) => {
  const h = await harness(t);
  const slug = "public-li";
  const postUrl = "https://www.linkedin.com/posts/good";
  const text = `${goodPostText().replace("acu-x", "acu-li")} <a href="/in/${slug}">`;
  h.mock.state.pages.set("/posts/good", { status: 200, body: text });
  const created = await h.claim(
    "linkedin",
    `https://www.linkedin.com/in/${slug}`,
    device(),
    postUrl,
  );
  assert.equal(created.data.status, "unlocked");
});

test("reject and expiry", async (t) => {
  const h = await harness(t);
  const created = await h.claim("x", "rejectme", device());
  await h.request(`/v1/admin/claims/${created.data.claim_id}/reject`, {
    admin: true,
    data: { reason: "spam" },
  });
  const polled = await h.poll(String(created.data.claim_id), created.device);
  assert.equal(polled.data.status, "rejected");
});

test("poll returns entitlement once unlocked", async (t) => {
  const h = await harness(t);
  const handle = "poll1";
  const postUrl = xPostUrl(handle, freshStatusId());
  seedGoodPost(h.mock.state, postUrl, handle, goodPostText());
  await h.store.atomic([
    {
      kind: "insert",
      table: "followers",
      row: { provider: "x", handle, x_user_id: "1" },
    },
  ]);
  const created = await h.claim("x", handle, device(), postUrl);
  const polled = await h.poll(String(created.data.claim_id), created.device);
  assert.ok(polled.data.entitlement);
});

test("one Mac device_taken", async (t) => {
  const h = await harness(t);
  const handleA = "maca";
  const handleB = "macb";
  const d = device();
  const postA = xPostUrl(handleA, freshStatusId());
  const postB = xPostUrl(handleB, freshStatusId());
  seedGoodPost(h.mock.state, postA, handleA, goodPostText());
  seedGoodPost(h.mock.state, postB, handleB, goodPostText());
  await h.store.atomic([
    {
      kind: "insert",
      table: "followers",
      row: { provider: "x", handle: handleA, x_user_id: "1" },
    },
    {
      kind: "insert",
      table: "followers",
      row: { provider: "x", handle: handleB, x_user_id: "2" },
    },
  ]);
  await h.claim("x", handleA, d, postA);
  const second = await h.claim("x", handleB, d, postB);
  assert.equal(second.status, 409);
  assert.equal(second.data.error, "device_taken");
});

test("device_moved on old device refresh after rebind unlock", async (t) => {
  const h = await harness(t);
  const handle = "rebind1";
  const oldDevice = device();
  const newDevice = device();
  const post1 = xPostUrl(handle, freshStatusId());
  seedGoodPost(h.mock.state, post1, handle, goodPostText());
  await h.store.atomic([
    {
      kind: "insert",
      table: "followers",
      row: { provider: "x", handle, x_user_id: "1" },
    },
  ]);
  await h.claim("x", handle, oldDevice, post1);
  const binding = (await h.store.list("bindings"))[0];
  await h.store.atomic([
    {
      kind: "update",
      table: "bindings",
      where: { account_id: binding.account_id },
      row: {
        last_rebind_at: new Date(Date.now() - 8 * 86400000).toISOString(),
      },
    },
  ]);
  const post2 = xPostUrl(handle, freshStatusId());
  seedGoodPost(h.mock.state, post2, handle, goodPostText());
  await h.claim("x", handle, newDevice, post2);
  const signed = signRequest(oldDevice, "refresh", {
    device_id: oldDevice.id,
  });
  const moved = await fetch(`${h.base}/v1/entitlement/refresh`, {
    method: "POST",
    headers: signed.headers,
    body: signed.body,
  });
  assert.equal((await moved.json()).error, "device_moved");
});

test("signature tampering and replay", async (t) => {
  const h = await harness(t);
  const d = device();
  const signed = signRequest(d, "claim", {
    provider: "x",
    handle: "sig1",
    post_url: null,
    device_pubkey: d.key,
    device_name: "Mac",
  });
  const badSig = { ...signed.headers, "x-acu-sig": "AAAA" };
  assert.equal(
    (
      await fetch(`${h.base}/v1/claims`, {
        method: "POST",
        headers: badSig,
        body: signed.body,
      })
    ).status,
    401,
  );
  const stale = signRequest(d, "claim", JSON.parse(signed.body));
  stale.headers["x-acu-ts"] = String(Math.floor(Date.now() / 1000) - 400);
  assert.equal(
    (
      await fetch(`${h.base}/v1/claims`, {
        method: "POST",
        headers: stale.headers,
        body: stale.body,
      })
    ).status,
    401,
  );
  await fetch(`${h.base}/v1/claims`, {
    method: "POST",
    headers: signed.headers,
    body: signed.body,
  });
  const replay = await fetch(`${h.base}/v1/claims`, {
    method: "POST",
    headers: signed.headers,
    body: signed.body,
  });
  assert.equal(replay.status, 401);
});

test("counter site-stats wall ref-click download admin stats", async (t) => {
  const h = await harness(t);
  const handle = "stats1";
  const postUrl = xPostUrl(handle, freshStatusId());
  seedGoodPost(h.mock.state, postUrl, handle, goodPostText());
  await h.store.atomic([
    {
      kind: "insert",
      table: "followers",
      row: { provider: "x", handle, x_user_id: "1" },
    },
  ]);
  const created = await h.claim("x", handle, device(), postUrl, true);
  await h.store.atomic([
    {
      kind: "update",
      table: "accounts",
      where: { id: (await h.store.list("accounts", { handle }))[0].id },
      row: { verified: true, verified_type: "blue", followers_count: 50 },
    },
  ]);
  assert.equal((await h.request("/v1/public/counter")).data.unlocks, 1);
  const wall = await h.request("/v1/public/site-stats");
  assert.equal(wall.data.downloads.github_dmg, 42);
  await h.request("/v1/public/ref-click?ref=acu-x");
  const stats = await h.request("/v1/admin/stats", { admin: true });
  assert.equal(stats.data.verified_unlocks, 1);
  assert.equal(stats.data.reach, 50);
});

test("admin auth 401 and backoff", async (t) => {
  const h = await harness(t);
  assert.equal((await h.request("/v1/admin/stats")).status, 401);
  for (let i = 0; i < 11; i += 1)
    await fetch(`${h.base}/v1/admin/stats`, {
      headers: { authorization: "Bearer wrong" },
    });
  const last = await fetch(`${h.base}/v1/admin/stats`, {
    headers: { authorization: "Bearer wrong" },
  });
  assert.equal(last.status, 429);
});

test("open claim reuse", async (t) => {
  const h = await harness(t);
  const d = device();
  const first = await h.claim("x", "reuse1", d);
  const second = await h.claim("x", "reuse1", d);
  assert.equal(first.data.claim_id, second.data.claim_id);
});

test("entitlement shape", async (t) => {
  const h = await harness(t);
  const handle = "ent1";
  const postUrl = xPostUrl(handle, freshStatusId());
  seedGoodPost(h.mock.state, postUrl, handle, goodPostText());
  await h.store.atomic([
    {
      kind: "insert",
      table: "followers",
      row: { provider: "x", handle, x_user_id: "1" },
    },
  ]);
  const created = await h.claim("x", handle, device(), postUrl);
  const [payload, sig] = String(created.data.entitlement).split(".");
  const key = createPublicKey({
    key: Buffer.from(h.gate.env.ENTITLEMENT_PRIVATE_KEY!, "base64"),
    format: "der",
    type: "pkcs8",
  });
  assert.equal(
    verify(null, Buffer.from(payload), key, Buffer.from(sig, "base64url")),
    true,
  );
  const claims = JSON.parse(Buffer.from(payload, "base64url").toString());
  assert.equal(claims.exp - claims.iat, 14 * 86400);
  assert.ok(snowflakeMs(freshStatusId()) > 0);
});
