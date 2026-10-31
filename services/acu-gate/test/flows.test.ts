import { test } from "node:test";
import assert from "node:assert/strict";
import { createPublicKey, verify } from "node:crypto";
import { readFile } from "node:fs/promises";
import { harness, device, signature } from "./helpers.ts";
import { sha256 } from "../src/crypto.ts";
import { tokenBlob } from "../src/settings.ts";

test("X OAuth → redeem → verify stores profile and signs an entitlement", async (t) => {
  const h = await harness(t),
    login = await h.login();
  assert.equal(login.status, 200);
  assert.equal(login.data.status, "needs_verify");
  const result = await h.request("/v1/verify/x", {
    session: login.data.session,
    data: {},
  });
  assert.equal(result.status, 200);
  assert.equal(result.data.status, "unlocked");
  const [payload, sig] = result.data.entitlement.split(".");
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
  assert.equal(claims.device_id, login.device.id);
  const profile = (await h.store.list("x_profiles"))[0];
  assert.equal(profile.verified, true);
  assert.equal(profile.verified_type, "blue");
  assert.equal(Number(profile.followers_count), 123);
  const stored = (await h.store.list("sessions"))[0];
  assert.equal(stored.token_hash, sha256(login.data.session));
  assert.equal(JSON.stringify(stored).includes(login.data.session), false);
  const replay = await h.request("/v1/session/redeem", {
    data: {
      code: login.code,
      app_verifier: login.verifier,
      device_id: login.device.id,
      device_pubkey: login.device.key,
    },
  });
  assert.equal(replay.status, 401);
});
for (const [user, error] of [
  ["no_follow", "follow_missing"],
  ["no_post", "post_missing"],
])
  test(`X ${error}`, async (t) => {
    const h = await harness(t),
      login = await h.login("x", user);
    const result = await h.request("/v1/verify/x", {
      session: login.data.session,
      data: {},
    });
    assert.equal(result.status, 422);
    assert.equal(result.data.error, error);
  });
for (const user of ["expanded", "hashtag"])
  test(`X matches ${user}`, async (t) => {
    const h = await harness(t),
      login = await h.login("x", user);
    assert.equal(
      (
        await h.request("/v1/verify/x", {
          session: login.data.session,
          data: {},
        })
      ).status,
      200,
    );
  });
for (const status of [402, 429, 500])
  test(`X ${status} maps to x_unavailable`, async (t) => {
    const h = await harness(t),
      login = await h.login();
    h.mock.state.xStatus = status;
    const r = await h.request("/v1/verify/x", {
      session: login.data.session,
      data: {},
    });
    assert.equal(r.status, 503);
    assert.equal(r.data.error, "x_unavailable");
  });
test("owner follower incremental sync stops at first known id", async (t) => {
  const h = await harness(t),
    login = await h.login();
  h.gate.env.FOLLOW_CHECK_MODE = "owner_followers";
  const identity = (await h.store.list("identities"))[0];
  const blob = JSON.parse(String(identity.token_blob));
  await h.store.atomic([
    { kind: "insert", table: "owner_tokens", row: { provider: "x", ...blob } },
    { kind: "insert", table: "owner_followers", row: { x_user_id: "known" } },
  ]);
  h.mock.state.followersPages = [["guru_test", "known", "never"], ["later"]];
  const r = await h.request("/v1/verify/x", {
    session: login.data.session,
    data: {},
  });
  assert.equal(r.status, 200);
  assert.equal(h.mock.state.followersCalls, 1);
  assert.equal(
    (await h.store.list("owner_followers", { x_user_id: "never" })).length,
    0,
  );
});
test("LinkedIn auto-pass, hashed email, URL uniqueness and validation", async (t) => {
  const h = await harness(t),
    a = await h.login("linkedin");
  const result = await h.request("/v1/verify/linkedin", {
    session: a.data.session,
    data: { post_url: "https://www.linkedin.com/posts/hello?tracking=one" },
  });
  assert.equal(result.status, 200);
  const identity = (await h.store.list("identities"))[0];
  assert.match(String(identity.email_hash), /^[a-f0-9]{64}$/);
  assert.equal(JSON.stringify(identity).includes("@example.invalid"), false);
  const b = await h.login("linkedin", "other");
  const reused = await h.request("/v1/verify/linkedin", {
    session: b.data.session,
    data: { post_url: "https://www.linkedin.com/posts/hello?tracking=two" },
  });
  assert.equal(reused.status, 422);
  assert.equal(reused.data.error, "post_url_used");
  for (const post_url of [
    "https://evil.test/posts/x",
    "http://www.linkedin.com/posts/x",
    "https://www.linkedin.com.evil.test/posts/x",
    "https://user@www.linkedin.com/posts/x",
    "https://www.linkedin.com:444/posts/x",
  ]) {
    const r = await h.request("/v1/verify/linkedin", {
      session: b.data.session,
      data: { post_url },
    });
    assert.equal(r.data.error, "post_url_invalid");
  }
});
for (const mode of ["mismatch", "wall", "redirect"])
  test(`LinkedIn ${mode} stays pending then admin approves and status binds`, async (t) => {
    const h = await harness(t),
      a = await h.login("linkedin");
    h.mock.pages.set(
      "/posts/review",
      mode === "mismatch"
        ? { author: "Someone Else" }
        : mode === "wall"
          ? { status: 999 }
          : { redirect: "https://evil.test/private" },
    );
    const result = await h.request("/v1/verify/linkedin", {
      session: a.data.session,
      data: { post_url: "https://www.linkedin.com/posts/review" },
    });
    assert.equal(result.status, 202);
    assert.equal(result.data.status, "pending_review");
    assert.equal(h.mock.state.pageCalls, 1);
    const pending = await h.request("/v1/admin/pending", { admin: true });
    assert.equal(pending.data.pending.length, 1);
    assert.equal(
      (
        await h.request(
          `/v1/admin/pending/${pending.data.pending[0].id}/approve`,
          { admin: true, data: {} },
        )
      ).status,
      200,
    );
    const status = await h.request("/v1/status", { session: a.data.session });
    assert.equal(status.data.status, "unlocked");
    assert.equal(typeof status.data.entitlement, "string");
    assert.equal((await h.store.list("unlocks")).length, 1);
  });
test("LinkedIn rejected claim remains rejected", async (t) => {
  const h = await harness(t),
    a = await h.login("linkedin");
  h.mock.pages.set("/posts/reject", { status: 999 });
  await h.request("/v1/verify/linkedin", {
    session: a.data.session,
    data: { post_url: "https://www.linkedin.com/posts/reject" },
  });
  const claims = await h.store.list("pending_claims");
  await h.request(`/v1/admin/pending/${claims[0].id}/reject`, {
    admin: true,
    data: {},
  });
  const r = await h.request("/v1/verify/linkedin", {
    session: a.data.session,
    data: { post_url: "https://www.linkedin.com/posts/reject" },
  });
  assert.equal(r.status, 422);
  assert.equal(r.data.error, "rejected");
});
test("one Mac, rebind cooldown, old device moves, reset and revocation", async (t) => {
  const h = await harness(t),
    a = await h.login();
  await h.request("/v1/verify/x", { session: a.data.session, data: {} });
  const taken = await h.login("x", "different", a.device);
  assert.equal(taken.data.error, "device_taken");
  const b = await h.login("x", "guru_test");
  const conflict = await h.request("/v1/status", { session: b.data.session });
  assert.equal(conflict.status, 409);
  assert.equal(conflict.data.error, "device_conflict");
  assert.equal(conflict.data.device_name, "Test Mac");
  const moved = await h.request("/v1/device/rebind", {
    session: b.data.session,
    data: signature(b.device, "rebind"),
  });
  assert.equal(moved.status, 200);
  const old = await h.request("/v1/entitlement/refresh", {
    data: signature(a.device, "refresh"),
  });
  assert.equal(old.status, 409);
  assert.equal(old.data.error, "device_moved");
  const c = await h.login("x", "guru_test");
  const cooldown = await h.request("/v1/device/rebind", {
    session: c.data.session,
    data: signature(c.device, "rebind"),
  });
  assert.equal(cooldown.status, 429);
  assert.equal(cooldown.data.error, "rebind_cooldown");
  const account = a.data.account.id;
  await h.request(`/v1/admin/accounts/${account}/reset-rebind`, {
    admin: true,
    data: {},
  });
  assert.equal(
    (
      await h.request("/v1/device/rebind", {
        session: c.data.session,
        data: signature(c.device, "rebind", Math.floor(Date.now() / 1000) + 1),
      })
    ).status,
    200,
  );
  await h.request(`/v1/admin/accounts/${account}/revoke`, {
    admin: true,
    data: {},
  });
  const revoked = await h.request("/v1/entitlement/refresh", {
    data: signature(c.device, "refresh", Math.floor(Date.now() / 1000) + 2),
  });
  assert.equal(revoked.status, 403);
  assert.equal(revoked.data.error, "revoked");
});
test("signed-in X user links LinkedIn into same account with one unlock", async (t) => {
  const h = await harness(t),
    a = await h.login();
  await h.request("/v1/verify/x", { session: a.data.session, data: {} });
  const linked = await h.login(
    "linkedin",
    "guru_test",
    a.device,
    a.data.session,
  );
  assert.equal(linked.status, 200);
  assert.equal(linked.data.account.id, a.data.account.id);
  assert.deepEqual(linked.data.account.linked.sort(), ["linkedin", "x"]);
  assert.equal(
    (
      await h.request("/v1/verify/linkedin", {
        session: linked.data.session,
        data: { post_url: "https://www.linkedin.com/posts/linked" },
      })
    ).status,
    200,
  );
  assert.equal((await h.store.list("unlocks")).length, 1);
});
test("refresh rejects bad signature, stale timestamp and replay", async (t) => {
  const h = await harness(t),
    a = await h.login();
  await h.request("/v1/verify/x", { session: a.data.session, data: {} });
  const valid = signature(a.device, "refresh");
  const bad = { ...valid, sig: signature(device(), "refresh").sig };
  assert.equal(
    (await h.request("/v1/entitlement/refresh", { data: bad })).data.error,
    "bad_signature",
  );
  assert.equal(
    (
      await h.request("/v1/entitlement/refresh", {
        data: signature(
          a.device,
          "refresh",
          Math.floor(Date.now() / 1000) - 301,
        ),
      })
    ).data.error,
    "stale_timestamp",
  );
  assert.equal(
    (await h.request("/v1/entitlement/refresh", { data: valid })).status,
    200,
  );
  assert.equal(
    (await h.request("/v1/entitlement/refresh", { data: valid })).data.error,
    "replay",
  );
});
test("counter, known refs, private traction stats and public rate limit", async (t) => {
  const h = await harness(t),
    a = await h.login(),
    b = await h.login("linkedin", "li");
  await h.request("/v1/verify/x", { session: a.data.session, data: {} });
  h.mock.pages.set("/posts/li", { author: "li" });
  await h.request("/v1/verify/linkedin", {
    session: b.data.session,
    data: { post_url: "https://www.linkedin.com/posts/li" },
  });
  const counter = await h.request("/v1/public/counter");
  assert.equal(counter.data.unlocks, 2);
  assert.equal(counter.headers.get("access-control-allow-origin"), "*");
  assert.equal(counter.headers.get("cache-control"), "public, max-age=60");
  for (const ref of ["acu-x", "acu-li", "unknown"])
    assert.equal(
      (await h.request(`/v1/public/ref-click?ref=${ref}`)).status,
      204,
    );
  assert.equal((await h.request("/v1/admin/stats")).status, 401);
  const stats = await h.request("/v1/admin/stats", { admin: true });
  assert.deepEqual(stats.data.unlocks, { x: 1, linkedin: 1, total: 2 });
  assert.equal(stats.data.verified_unlocks, 1);
  assert.equal(stats.data.verified_by_type.blue, 1);
  assert.equal(stats.data.reach, 123);
  assert.deepEqual(stats.data.ref_clicks, { "acu-x": 1, "acu-li": 1 });
  let limited = false;
  for (let i = 0; i < 61; i++)
    if ((await h.request("/v1/public/counter")).status === 429) limited = true;
  assert.equal(limited, true);
});
test(
  [
    "wall consent is private by default, opts in, removes immediately",
    "and supports device signatures",
  ].join(" "),
  async (t) => {
    const h = await harness(t),
      a = await h.login();
    await h.request("/v1/verify/x", { session: a.data.session, data: {} });
    assert.equal(
      (await h.request("/v1/public/site-stats")).data.wall.length,
      0,
    );
    assert.equal(
      (
        await h.request("/v1/wall", {
          session: a.data.session,
          data: { show: true },
        })
      ).status,
      200,
    );
    let stats = await h.request("/v1/public/site-stats");
    assert.equal(stats.data.wall.length, 1);
    assert.equal(stats.data.wall[0].profile_url, "https://x.com/guru_test");
    assert.equal(
      stats.data.wall[0].avatar_url,
      "https://pbs.twimg.com/profile_images/test.png",
    );
    assert.equal(
      (
        await h.request("/v1/wall", {
          data: signature(
            a.device,
            "wall",
            Math.floor(Date.now() / 1000),
            false,
          ),
        })
      ).status,
      200,
    );
    assert.equal(
      (await h.request("/v1/public/site-stats")).data.wall.length,
      0,
    );
  },
);
test("unlock consent is honored and invalid avatar host is dropped", async (t) => {
  const h = await harness(t);
  h.mock.user("guru_test").avatar = "https://evil.test/a.png";
  const a = await h.login();
  await h.request("/v1/verify/x", {
    session: a.data.session,
    data: { show_on_wall: true },
  });
  const stats = await h.request("/v1/public/site-stats");
  assert.equal(stats.data.wall.length, 1);
  assert.equal(stats.data.wall[0].avatar_url, null);
  assert.equal(
    (await h.request("/v1/admin/stats", { admin: true })).data.wall_opt_in,
    1,
  );
});
test("site snippets never use innerHTML", async () => {
  const source = await readFile(
    new URL("../site/site-stats-snippet.html", import.meta.url),
    "utf8",
  );
  assert.equal(/innerHTML/.test(source), false);
  assert.match(source, /textContent/);
  assert.match(source, /setAttribute/);
});

test("single-use code rejects wrong challenge and concurrent redemption", async (t) => {
  const h = await harness(t),
    a = await h.authorize();
  const data = {
    code: a.code,
    app_verifier: a.verifier,
    device_id: a.device.id,
    device_pubkey: a.device.key,
  };
  assert.equal(
    (
      await h.request("/v1/session/redeem", {
        data: { ...data, app_verifier: "wrong" },
      })
    ).status,
    401,
  );
  const results = await Promise.all([
    h.request("/v1/session/redeem", { data }),
    h.request("/v1/session/redeem", { data }),
  ]);
  assert.deepEqual(results.map((r) => r.status).sort(), [200, 401]);
  assert.equal((await h.store.list("sessions")).length, 1);
});
test("simultaneous unlocks on the same account converge on one entitlement", async (t) => {
  const h = await harness(t),
    a = await h.login();
  const results = await Promise.all([
    h.request("/v1/verify/x", { session: a.data.session, data: {} }),
    h.request("/v1/verify/x", { session: a.data.session, data: {} }),
  ]);
  assert.deepEqual(
    results.map((r) => r.status),
    [200, 200],
  );
  assert.equal((await h.store.list("unlocks")).length, 1);
  assert.equal((await h.store.list("bindings")).length, 1);
});
test("simultaneous claims on one LinkedIn post cannot unlock two accounts", async (t) => {
  const h = await harness(t),
    a = await h.login("linkedin", "claim_a"),
    b = await h.login("linkedin", "claim_b");
  h.mock.pages.set("/posts/race", { status: 999 });
  const results = await Promise.all([
    h.request("/v1/verify/linkedin", {
      session: a.data.session,
      data: { post_url: "https://www.linkedin.com/posts/race" },
    }),
    h.request("/v1/verify/linkedin", {
      session: b.data.session,
      data: { post_url: "https://www.linkedin.com/posts/race" },
    }),
  ]);
  assert.deepEqual(results.map((r) => r.status).sort(), [202, 422]);
  assert.equal(
    results.find((r) => r.status === 422)?.data.error,
    "post_url_used",
  );
  assert.equal((await h.store.list("pending_claims")).length, 1);
});
test("expired sessions and states fail, and expiry cleanup deletes stale replay records", async (t) => {
  const h = await harness(t),
    a = await h.login();
  const stored = (await h.store.list("sessions"))[0];
  await h.store.atomic([
    {
      kind: "update",
      table: "sessions",
      where: { token_hash: stored.token_hash },
      row: { expires_at: new Date(0).toISOString() },
    },
    {
      kind: "insert",
      table: "device_replays",
      row: {
        device_id: a.device.id,
        ts: 1,
        expires_at: new Date(0).toISOString(),
      },
    },
  ]);
  const expired = await h.request("/v1/status", { session: a.data.session });
  assert.equal(expired.status, 401);
  assert.equal(expired.data.error, "session_expired");
  await h.store.purgeExpired(new Date().toISOString());
  assert.equal((await h.store.list("device_replays")).length, 0);
});
