import { test } from "node:test";
import assert from "node:assert/strict";
import { harness, device, signRequest, xPostUrl, goodPostText, assertSameSecondReplay } from "./helpers.ts";
import { seedGoodPost } from "../src/mock-services.ts";
import { MemoryStore } from "../src/store.ts";
import { authorHandleFromOEmbedUrl } from "../src/handles.ts";

function freshStatusId() {
  return ((BigInt(Date.now() - 1288834974657) << 22n) + 1n).toString();
}

test("oEmbed author_url must be on X hosts", () => {
  assert.equal(authorHandleFromOEmbedUrl("https://evil.test/alice"), "");
  assert.equal(authorHandleFromOEmbedUrl("https://x.com/alice"), "alice");
});

test("oEmbed spoofed author_url on foreign host fails post check", async (t) => {
  const h = await harness(t);
  const handle = "alice";
  const postUrl = xPostUrl(handle, freshStatusId());
  h.mock.state.oembed.set(postUrl, {
    status: 200,
    author_url: `https://evil.test/${handle}`,
    html: `<p>${goodPostText()}</p>`,
  });
  const created = await h.claim("x", handle, device(), postUrl);
  assert.equal(created.data.checks.post, "failed");
});

test("claim body hash must match signed bytes", async (t) => {
  const h = await harness(t);
  const d = device();
  const signed = signRequest(d, "claim", {
    provider: "x",
    handle: "bodyhash",
    post_url: null,
    device_pubkey: d.key,
    device_name: "Mac",
  });
  const tampered = JSON.stringify({
    ...JSON.parse(signed.body),
    device_name: "Other Mac",
  });
  const response = await fetch(`${h.base}/v1/claims`, {
    method: "POST",
    headers: signed.headers,
    body: tampered,
  });
  assert.equal(response.status, 401);
});

test("claim rate limit per IP", async (t) => {
  const h = await harness(t);
  let limited = { status: 200 };
  for (let i = 0; i < 8; i += 1) {
    const d = device();
    const signed = signRequest(d, "claim", {
      provider: "x",
      handle: `ip${i}`,
      post_url: null,
      device_pubkey: d.key,
      device_name: "Mac",
    });
    limited = await fetch(`${h.base}/v1/claims`, {
      method: "POST",
      headers: signed.headers,
      body: signed.body,
    });
    if (limited.status === 429) break;
  }
  assert.equal(limited.status, 429);
  assert.equal((await limited.json()).error, "rate_limited");
});

test("claim rate limit per device daily", async (t) => {
  const store = new MemoryStore();
  const h = await harness(t, store);
  const d = device();
  const day = new Date().toISOString().slice(0, 10);
  await store.atomic([
    {
      kind: "insert",
      table: "claim_device_limits",
      row: { device_id: d.id, day, count: 10 },
    },
  ]);
  const signed = signRequest(d, "claim", {
    provider: "x",
    handle: "limited",
    post_url: null,
    device_pubkey: d.key,
    device_name: "Mac",
  });
  const limited = await fetch(`${h.base}/v1/claims`, {
    method: "POST",
    headers: signed.headers,
    body: signed.body,
  });
  assert.equal(limited.status, 429);
  assert.equal((await limited.json()).error, "rate_limited");
});

test("poll rate limit per device", async (t) => {
  const h = await harness(t);
  const handle = "pollrate";
  const d = device();
  const created = await h.claim("x", handle, d);
  let limited = { status: 200 };
  for (let i = 0; i < 62; i += 1) {
    limited = await h.poll(String(created.data.claim_id), d);
    if (limited.status === 429) break;
  }
  assert.equal(limited.status, 429);
});

test("wall consent requires valid signature", async (t) => {
  const h = await harness(t);
  const handle = "wall1";
  const d = device();
  const postUrl = xPostUrl(handle, freshStatusId());
  seedGoodPost(h.mock.state, postUrl, handle, goodPostText());
  await h.store.atomic([
    {
      kind: "insert",
      table: "followers",
      row: { provider: "x", handle, x_user_id: "1" },
    },
  ]);
  await h.claim("x", handle, d, postUrl);
  const payload = { device_id: d.id, show: true };
  const signed = signRequest(d, "wall", payload);
  const ok = await fetch(`${h.base}/v1/wall`, {
    method: "POST",
    headers: signed.headers,
    body: signed.body,
  });
  assert.equal(ok.status, 200);
});

test("same-second claim and poll succeed; replay is rejected", async (t) => {
  await assertSameSecondReplay(await harness(t));
});
