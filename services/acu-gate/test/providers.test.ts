import { test } from "node:test";
import assert from "node:assert/strict";
import { LinkedInProvider } from "../src/providers.ts";
import { startMocks } from "../src/mock-providers.ts";
import { harness, device } from "./helpers.ts";
import { pkce, opaque } from "../src/providers.ts";
test("LinkedIn fetch bounds: three same-host redirects, no cookies, and two MB limit", async (t) => {
  const mock = await startMocks();
  t.after(mock.close);
  let observed = false;
  const provider = new LinkedInProvider(mock.env, (url, init) => {
    assert.equal(init.credentials, "omit");
    assert.equal(new Headers(init.headers).has("cookie"), false);
    assert.equal(init.redirect, "manual");
    assert.ok(init.signal);
    observed = true;
    return mock.pageFetch(url, init);
  });
  for (let i = 0; i < 3; i++)
    mock.pages.set(`/posts/r${i}`, {
      redirect: `https://www.linkedin.com/posts/r${i + 1}`,
    });
  assert.equal(
    await provider.verifyPage(
      "https://www.linkedin.com/posts/r0",
      "  GURU   TEST ",
      (s) => s.includes("#AgentComputerUse"),
    ),
    true,
  );
  assert.equal(observed, true);
  mock.pages.set("/posts/r3", {
    redirect: "https://www.linkedin.com/posts/r4",
  });
  assert.equal(
    await provider.verifyPage(
      "https://www.linkedin.com/posts/r0",
      "Guru Test",
      () => true,
    ),
    false,
  );
  mock.pages.set("/posts/huge", {
    body:
      '<meta name="author" content="Guru Test">#AgentComputerUse' +
      "x".repeat(2 * 1024 * 1024),
  });
  assert.equal(
    await provider.verifyPage(
      "https://www.linkedin.com/posts/huge",
      "Guru Test",
      () => true,
    ),
    false,
  );
});
test("X verified is metadata and is never required to unlock", async (t) => {
  const h = await harness(t);
  h.mock.user("guru_test").verified = false;
  const a = await h.login();
  assert.equal(
    (await h.request("/v1/verify/x", { session: a.data.session, data: {} }))
      .status,
    200,
  );
  const stats = await h.request("/v1/admin/stats", { admin: true });
  assert.equal(stats.data.verified_unlocks, 0);
  assert.equal(stats.data.reach, 123);
});
test("admin owner OAuth stores encrypted owner credentials and rejects a different user", async (t) => {
  const h = await harness(t);
  for (const [user, status] of [
    ["not_owner", 302],
    ["owner", 200],
  ] as const) {
    const start = await h.request(`/v1/admin/x-owner/start?mock_user=${user}`, {
      admin: true,
    });
    assert.equal(start.status, 302);
    const authorizeUrl = new URL(start.headers.get("location")!);
    assert.equal(
      authorizeUrl.searchParams.get("scope"),
      "tweet.read users.read follows.read offline.access",
    );
    const authorized = await fetch(authorizeUrl, {
      redirect: "manual",
    });
    await authorized.arrayBuffer();
    const callback = await fetch(authorized.headers.get("location")!, {
      redirect: "manual",
    });
    await callback.arrayBuffer();
    assert.equal(callback.status, status);
    if (user === "not_owner")
      assert.match(callback.headers.get("location")!, /owner_mismatch/);
  }
  const tokens = await h.store.list("owner_tokens");
  assert.equal(tokens.length, 1);
  assert.equal(typeof tokens[0].ciphertext, "string");
  assert.equal(typeof tokens[0].nonce, "string");
  assert.equal(typeof tokens[0].tag, "string");
  assert.equal(typeof tokens[0].refresh_ciphertext, "string");
  assert.equal(typeof tokens[0].access_expires_at, "string");
  assert.equal(tokens[0].owner_auth_required, false);
});
test("user X OAuth omits offline.access", async (t) => {
  const h = await harness(t);
  const d = device();
  const verifier = opaque();
  const start = await h.request(
    `/v1/auth/x/start?device_id=${d.id}&device_name=Test&app_challenge=${pkce(verifier)}&mock_user=guru_test`,
  );
  assert.equal(start.status, 302);
  const scope = new URL(start.headers.get("location")!).searchParams.get(
    "scope",
  );
  assert.equal(scope, "tweet.read users.read follows.read");
  assert.equal(scope?.includes("offline.access"), false);
});
async function authorizeOwner(h: Awaited<ReturnType<typeof harness>>) {
  const start = await h.request("/v1/admin/x-owner/start?mock_user=owner", {
    admin: true,
  });
  assert.equal(start.status, 302);
  const approved = await fetch(start.headers.get("location")!, {
    redirect: "manual",
  });
  await approved.arrayBuffer();
  const callback = await fetch(approved.headers.get("location")!, {
    redirect: "manual",
  });
  await callback.arrayBuffer();
  assert.equal(callback.status, 200);
}
test("owner token refreshes when near expiry and stores the rotated refresh token", async (t) => {
  const h = await harness(t, undefined, { FOLLOW_CHECK_MODE: "owner_followers" });
  await authorizeOwner(h);
  const before = (await h.store.list("owner_tokens"))[0];
  await h.store.atomic([
    {
      kind: "update",
      table: "owner_tokens",
      where: { provider: "x" },
      row: {
        access_expires_at: new Date(Date.now() + 60_000).toISOString(),
      },
    },
  ]);
  h.mock.state.followersPages = [["guru_test"]];
  const login = await h.login();
  const result = await h.request("/v1/verify/x", {
    session: login.data.session,
    data: {},
  });
  assert.equal(result.status, 200);
  const after = (await h.store.list("owner_tokens"))[0];
  assert.notEqual(after.refresh_ciphertext, before.refresh_ciphertext);
  assert.equal(after.owner_auth_required, false);
});
test("owner refresh failure sets owner_auth_required and maps to x_unavailable", async (t) => {
  const h = await harness(t, undefined, { FOLLOW_CHECK_MODE: "owner_followers" });
  await authorizeOwner(h);
  await h.store.atomic([
    {
      kind: "update",
      table: "owner_tokens",
      where: { provider: "x" },
      row: {
        access_expires_at: new Date(Date.now() + 60_000).toISOString(),
      },
    },
  ]);
  h.mock.state.refreshFail = true;
  h.mock.state.followersPages = [["guru_test"]];
  const login = await h.login();
  const result = await h.request("/v1/verify/x", {
    session: login.data.session,
    data: {},
  });
  assert.equal(result.status, 503);
  assert.equal(result.data.error, "x_unavailable");
  const stats = await h.request("/v1/admin/stats", { admin: true });
  assert.equal(stats.data.owner_auth_required, true);
});
