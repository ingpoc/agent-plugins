import { test } from "node:test";
import assert from "node:assert/strict";
import { LinkedInProvider } from "../src/providers.ts";
import { startMocks } from "../src/mock-providers.ts";
import { harness } from "./helpers.ts";
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
    const authorized = await fetch(start.headers.get("location")!, {
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
});
