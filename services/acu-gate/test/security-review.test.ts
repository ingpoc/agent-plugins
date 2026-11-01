import { test } from "node:test";
import assert from "node:assert/strict";
import { harness, device } from "./helpers.ts";
import { MemoryStore, type Op } from "../src/store.ts";
import { preset } from "../src/settings.ts";

test("a signing key cannot obtain trials under alternate device IDs", async (t) => {
  const h = await harness(t);
  const d = device();
  const first = await h.claim("linkedin", "trial-a", d);
  assert.equal(first.status, 201);
  const second = await h.claim("linkedin", "trial-b", { ...d, id: "f".repeat(64) });
  assert.equal(second.status, 409);
  assert.equal(second.data.error, "device_conflict");
  assert.equal((await h.store.list("trials")).length, 1);
});

test("an X claim without a trial still reserves its signing key to one device ID", async (t) => {
  const h = await harness(t);
  const d = device();
  const first = await h.claim("x", "pendingkey", d);
  assert.equal(first.status, 201);
  assert.equal((await h.store.list("trials")).length, 0);
  const second = await h.claim("linkedin", "another-key", { ...d, id: "e".repeat(64) });
  assert.equal(second.status, 409);
  assert.equal(second.data.error, "device_conflict");
});

test("trial uniqueness is shared across accounts and providers", async (t) => {
  const h = await harness(t);
  const d = device();
  const first = await h.claim("linkedin", "trial-a", d);
  assert.equal(first.status, 201);
  const duplicate = await h.claim("linkedin", "trial-b", d);
  assert.equal(duplicate.status, 201);
  assert.equal((await h.store.list("trials")).length, 1);
});

test("expired trials remain consumed and signing-key encodings canonicalize", async (t) => {
  const h = await harness(t);
  const d = device();
  const first = await h.claim("linkedin", "expired-trial", d);
  assert.equal(first.status, 201);
  await h.store.atomic([{ kind: "update", table: "trials", where: { claim_id: first.data.claim_id }, row: { ends_at: new Date(Date.now() - 86400000).toISOString() } }]);
  await h.store.purgeExpired(new Date(Date.now() + 365 * 86400000).toISOString());
  assert.equal((await h.store.list("trials", { claim_id: first.data.claim_id })).length, 1);
  const alternateEncoding = { ...d, id: "d".repeat(64), key: d.key.replace(/=+$/, "") };
  const blocked = await h.claim("linkedin", "expired-trial-other", alternateEncoding);
  assert.equal(blocked.status, 409);
  assert.equal(blocked.data.error, "device_conflict");
});

test("configured launch history remains permanent after config is removed", async (t) => {
  for (const [provider, env, handle] of [
    ["x", { GATE_X_LAUNCH_POST_ID: "123456789" }, "old_trialx"],
    ["linkedin", { GATE_LINKEDIN_LAUNCH_POST_URL: "https://www.linkedin.com/posts/alice-test" }, "old-trial-li"],
  ] as const) await t.test(provider, async (t) => {
    const store = new MemoryStore();
    const old = new Date(Date.now() - 30 * 86400000).toISOString();
    await store.atomic([{ kind: "insert", table: "gate_settings", row: { key: `${provider}_launch_first_seen_at`, value: old, updated_at: old } }]);
    const first = await harness(t, store, { ...env, ...(provider === "x" ? { GATE_X_LAUNCH_POST_ID: "123456789" } : {}) });
    if (provider === "x") await store.atomic([{ kind: "insert", table: "followers", row: { provider: "x", handle, x_user_id: "22" } }]);
    const created = await first.claim(provider, handle);
    assert.equal(created.data.status, "trial");
    const expired = new Date(Date.now() - 1000).toISOString();
    await store.atomic([{ kind: "update", table: "trials", where: { claim_id: created.data.claim_id }, row: { ends_at: expired } }]);
    const second = await harness(t, store, {});
    const reread = await second.poll(String(created.data.claim_id), created.device);
    assert.notEqual(reread.data.status, "trial");
  });
});

class BindingBarrierStore extends MemoryStore {
  private arrivals = 0;
  private release!: () => void;
  private gate = new Promise<void>((resolve) => { this.release = resolve; });
  override async atomic(ops: Op[]) {
    if (ops.some((op) => op.kind === "update" && op.table === "bindings" && op.row.device_id !== undefined)) {
      this.arrivals++;
      if (this.arrivals === 2) this.release();
      await this.gate;
    }
    return super.atomic(ops);
  }
}

test("concurrent destination claims cannot both rebind and unlock", async (t) => {
  const store = new BindingBarrierStore();
  const h = await harness(t, store, { GATE_X_LAUNCH_POST_ID: "123456789" });
  const now = new Date().toISOString();
  const oldDate = new Date(Date.now() - 8 * 86400000).toISOString();
  const d1 = device(), d2 = device();
  const account = { id: "acct-race", provider: "x", handle: "raceuser", x_user_id: "5544", created_at: now };
  const claims = [
    { id: "claim-race-1", device_id: d1.id, device_pubkey: d1.key, device_name: "one" },
    { id: "claim-race-2", device_id: d2.id, device_pubkey: d2.key, device_name: "two" },
  ].map((claim) => ({ ...claim, account_id: account.id, post_url: null, post_key: null, follow_check: "confirmed", post_check: "pending", status: "pending_verification", created_at: now, expires_at: new Date(Date.now() + 86400000).toISOString() }));
  await store.atomic([
    { kind: "insert", table: "accounts", row: account },
    { kind: "insert", table: "bindings", row: { account_id: account.id, device_id: "a".repeat(64), device_pubkey: "old-key", device_name: "old", bound_at: oldDate, last_rebind_at: oldDate } },
    ...claims.map((row) => ({ kind: "insert" as const, table: "claims" as const, row })),
  ]);
  const verify = (id: string) => h.request(`/v1/admin/claims/${id}/verify`, { admin: true, data: { follow_confirmed: true, repost_confirmed: true, source: "x_plugin" } });
  const results = await Promise.all([verify("claim-race-1"), verify("claim-race-2")]);
  assert.equal(results.filter((result) => result.data.status === "unlocked").length, 1);
  assert.equal(results.filter((result) => result.data.reason === "rebind_cooldown").length, 1);
  assert.equal((await store.list("used_posts")).length, 1);
});

test("reposter snapshot rejects malformed entries and conflicting identities", async (t) => {
  for (const kind of ["followers", "reposts"] as const) await t.test(kind, async (t) => {
    const h = await harness(t, new MemoryStore(), { GATE_X_LAUNCH_POST_ID: "123456789" });
    const common = { provider: "x", source: "x_plugin", post_id: "123456789" };
    await h.store.atomic([
      { kind: "insert", table: "accounts", row: { id: "alice-account", provider: "x", handle: "alice", x_user_id: "111" } },
      { kind: "insert", table: "accounts", row: { id: "bob-account", provider: "x", handle: "bob", x_user_id: "222" } },
      { kind: "insert", table: "followers", row: { provider: "x", handle: "alice", x_user_id: "111" } },
      { kind: "insert", table: "followers", row: { provider: "x", handle: "bob", x_user_id: "222" } },
    ]);
    const path = kind === "reposts" ? "/v1/admin/reposts" : "/v1/admin/followers";
    for (const row of [
      { id: 123, handle: "alice" },
      { id: null, handle: "alice" },
      { id: ["1"], handle: "alice" },
      { id: "1", handle: "bad handle" },
    ]) {
      const data = kind === "reposts" ? { ...common, reposters: [row] } : { followers: [row] };
      const res = await h.request(path, { admin: true, data });
      assert.equal(res.status, 400);
      assert.equal(res.data.error, "invalid_request");
    }
    const data = kind === "reposts" ? { ...common, reposters: [{ id: "222", handle: "alice" }] } : { followers: [{ id: "222", handle: "alice" }] };
    const conflict = await h.request(path, { admin: true, data });
    assert.equal(conflict.status, 409);
    assert.equal(conflict.data.error, "identity_conflict");
  });
});

test("reposter snapshot size is bounded", async (t) => {
  const h = await harness(t, new MemoryStore(), { GATE_X_LAUNCH_POST_ID: "123456789" });
  const reposters = Array.from({ length: 5001 }, () => null);
  const res = await h.request("/v1/admin/reposts", { admin: true, data: { provider: "x", source: "x_plugin", post_id: "123456789", reposters } });
  assert.equal(res.status, 400);
  assert.equal(res.data.error, "invalid_request");
  const rawLimit = await h.request("/v1/admin/reposts", { admin: true, body: JSON.stringify({ provider: "x", source: "x_plugin", post_id: "123456789", reposters: [{ id: "1", handle: "a", extra: "x".repeat(200000) }] }) });
  assert.equal(rawLimit.status, 413);
});

test("authenticated admin requests are rate limited per source", async (t) => {
  const h = await harness(t);
  let last;
  for (let i = 0; i < 121; i++) last = await h.request("/v1/admin/claims", { admin: true });
  assert.equal(last?.status, 429);
});

test("follower and repost pushes process at most 500 pending claims", async (t) => {
  for (const kind of ["followers", "reposts"] as const) await t.test(kind, async (t) => {
    const h = await harness(t, new MemoryStore(), { GATE_X_LAUNCH_POST_ID: "123456789" });
    const now = new Date().toISOString();
    const accounts = Array.from({ length: 501 }, (_, i) => ({ id: `a${i}`, provider: "x", handle: `fan${i}`, x_user_id: String(i + 1), created_at: now }));
    const claims = Array.from({ length: 501 }, (_, i) => ({ id: `c${i}`, account_id: `a${i}`, device_id: `${i.toString(16).padStart(64, "0")}`, device_pubkey: "test-key", device_name: "test", status: "pending_verification", follow_check: "pending", post_check: "pending", created_at: now, expires_at: new Date(Date.now() + 30 * 86400000).toISOString() }));
    await h.store.atomic([
      ...accounts.map((row) => ({ kind: "insert" as const, table: "accounts" as const, row })),
      ...claims.map((row) => ({ kind: "insert" as const, table: "claims" as const, row })),
    ]);
    if (kind === "followers") {
      const followers = accounts.map(({ handle, x_user_id }) => ({ handle, id: x_user_id }));
      const result = await h.request("/v1/admin/followers", { admin: true, data: { followers } });
      assert.equal((result.data.follow_confirmed_claims as string[]).length, 500);
    } else {
      const reposters = accounts.map(({ handle, x_user_id }) => ({ handle, id: x_user_id }));
      const result = await h.request("/v1/admin/reposts", { admin: true, data: { provider: "x", source: "x_plugin", post_id: "123456789", reposters } });
      assert.equal((result.data.results as unknown[]).length, 500);
    }
    assert.equal((await h.store.list("claims", { status: "pending_verification" })).length, 501);
  });
});

test("repost rows cannot cross-match established accounts by handle", async (t) => {
  const h = await harness(t, new MemoryStore(), { GATE_X_LAUNCH_POST_ID: "123456789" });
  await h.store.atomic([
    { kind: "insert", table: "accounts", row: { id: "alice-account", provider: "x", handle: "alice", x_user_id: "111" } },
    { kind: "insert", table: "accounts", row: { id: "bob-account", provider: "x", handle: "bob", x_user_id: "222" } },
  ]);
  await h.store.atomic([{ kind: "insert", table: "followers", row: { provider: "x", handle: "alice", x_user_id: "111" } }]);
  const res = await h.request("/v1/admin/reposts", { admin: true, data: { provider: "x", source: "x_plugin", post_id: "123456789", reposters: [{ id: "222", handle: "alice" }] } });
  assert.equal(res.status, 409);
  assert.equal(res.data.error, "identity_conflict");
});

test("concurrent snapshot assignments cannot establish one X ID on different handles", async (t) => {
  class IdentityBarrierStore extends MemoryStore {
    private arrivals = 0;
    private release!: () => void;
    private gate = new Promise<void>((resolve) => { this.release = resolve; });
    override async atomic(ops: Op[]) {
      if (ops.some((op) => op.kind === "insert" && op.table === "x_identities")) {
        if (++this.arrivals === 2) this.release();
        await this.gate;
      }
      return super.atomic(ops);
    }
  }
  const h = await harness(t, new IdentityBarrierStore());
  await h.claim("x", "pin_alice");
  await h.claim("x", "pin_bob");
  const results = await Promise.all(["pin_alice", "pin_bob"].map((handle) => h.request("/v1/admin/followers", {
    admin: true, data: { followers: [{ id: "87654", handle }] },
  })));
  assert.deepEqual(results.map((result) => result.status).sort(), [200, 409]);
  assert.equal((await h.store.list("accounts", { provider: "x", x_user_id: "87654" })).length, 1);
  assert.equal((await h.store.list("followers", { x_user_id: "87654" })).length, 1);
});

test("an already-running prelaunch gate observes launch history written by another gate", async (t) => {
  const store = new MemoryStore();
  const h = await harness(t, store);
  const created = await h.claim("linkedin", "history-refresh");
  const seen = new Date(Date.now() - 5 * 86400000).toISOString();
  const ended = new Date(Date.now() - 1000).toISOString();
  await store.atomic([
    { kind: "insert", table: "gate_settings", row: { key: "linkedin_launch_first_seen_at", value: seen, updated_at: seen } },
    { kind: "update", table: "trials", where: { claim_id: created.data.claim_id }, row: { ends_at: ended } },
  ]);
  const response = await h.poll(String(created.data.claim_id), created.device);
  assert.equal(response.data.status, "trial_expired");
  assert.equal(response.data.entitlement, null);
  assert.equal((await store.list("trials", { claim_id: created.data.claim_id }))[0].ends_at, ended);
});
