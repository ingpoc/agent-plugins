import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { MemoryStore, NeonStore, StoreConflict, tables, type Op, type Row } from "../src/store.ts";

const oldBinding: Row = {
  account_id: "account", device_id: "old-device", device_pubkey: "key-old", device_name: "Old Mac",
  bound_at: "2026-01-01T00:00:00.000Z", last_rebind_at: null,
};
function rebindOps(device: string): Op[] {
  return [
    { kind: "insert", table: "used_posts", row: { post_key: device, account_id: "account", created_at: "2026-11-01T00:00:00.000Z" } },
    { kind: "update", table: "bindings", where: {
      account_id: oldBinding.account_id, device_id: oldBinding.device_id,
      bound_at: oldBinding.bound_at, last_rebind_at: oldBinding.last_rebind_at,
    }, row: { device_id: device, bound_at: "2026-11-01T00:00:00.000Z", last_rebind_at: "2026-11-01T00:00:00.000Z" }, required: true },
  ];
}

test("Memory binding CAS rejects each changed observed field and rolls back post consumption", async () => {
  for (const field of ["device_id", "bound_at", "last_rebind_at"] as const) {
    const store = new MemoryStore();
    await store.atomic([{ kind: "insert", table: "bindings", row: oldBinding }]);
    await store.atomic([{ kind: "update", table: "bindings", where: { account_id: "account" }, row: {
      [field]: field === "device_id" ? "winner" : "2026-10-31T00:00:00.000Z",
    } }]);
    await assert.rejects(store.atomic(rebindOps("loser")), StoreConflict);
    assert.equal((await store.list("used_posts")).length, 0, `${field} conflict must roll back the post reservation`);
  }
});

test("Neon binding CAS uses every observed field, null-safe predicates and one transaction", async () => {
  const store = new NeonStore("postgres://local:local@localhost/test");
  const calls: Array<{ query: string; params: unknown[] }> = [];
  const transactions: unknown[][] = [];
  let fail = false;
  (store as unknown as { sql: unknown }).sql = {
    query(query: string, params: unknown[]) { const call = { query, params }; calls.push(call); return call; },
    async transaction(queries: unknown[]) {
      transactions.push(queries);
      if (fail) throw { code: "P0001" };
      return [];
    },
  };
  await store.atomic(rebindOps("winner"));
  assert.equal(transactions.length, 1);
  assert.equal(transactions[0].length, 2);
  assert.match(calls[0].query, /INSERT INTO .*used_posts/);
  assert.doesNotMatch(calls[0].query, /ON CONFLICT DO NOTHING/);
  const binding = calls[1];
  for (const field of ["account_id", "device_id", "bound_at", "last_rebind_at"]) {
    assert.match(binding.query, new RegExp(`"${field}" IS NOT DISTINCT FROM \\$\\d+`));
  }
  assert.match(binding.query, /acu_require\(count\(\*\)::integer\)/);
  assert.equal(binding.params.at(-1), null);
  fail = true;
  await assert.rejects(store.atomic(rebindOps("loser")), StoreConflict);
});

test("trial public-key uniqueness is permanent and nullable legacy rows have SQL semantics", async () => {
  const store = new MemoryStore();
  for (const suffix of ["one", "two"]) {
    await store.atomic([{ kind: "insert", table: "trials", row: { id: suffix, account_id: suffix, device_id: suffix, device_pubkey: null } }]);
  }
  await store.atomic([{ kind: "insert", table: "trials", row: { id: "three", account_id: "three", device_id: "three", device_pubkey: "signing-key" } }]);
  await store.purgeExpired("2099-01-01T00:00:00.000Z");
  await assert.rejects(store.atomic([{ kind: "insert", table: "trials", row: { id: "four", account_id: "four", device_id: "four", device_pubkey: "signing-key" } }]), StoreConflict);
  assert.equal((await store.list("trials")).length, 3);
});

test("device-key migration is additive and retains consumed legacy trial records", async () => {
  const sql = await readFile(new URL("../migrations/003_device_keys.sql", import.meta.url), "utf8");
  assert.match(sql, /ALTER TABLE trials ADD COLUMN IF NOT EXISTS device_pubkey text/);
  assert.match(sql, /CREATE UNIQUE INDEX IF NOT EXISTS trials_device_pubkey_unique ON trials \(device_pubkey\)/);
  assert.match(sql, /CREATE TABLE IF NOT EXISTS device_keys/);
  assert.match(sql, /ON CONFLICT \(device_pubkey\) DO NOTHING/);
  assert.match(sql, /row_number\(\) OVER/);
  assert.doesNotMatch(sql, /\b(?:DELETE|DROP|TRUNCATE)\b/i);
  assert.ok(tables.trials.includes("device_pubkey"));
  const calls: string[] = [];
  const store = new NeonStore("postgres://local:local@localhost/test");
  (store as unknown as { sql: unknown }).sql = {
    query(query: string) { calls.push(query); return Promise.resolve([]); },
    async transaction(queries: Promise<unknown>[]) { await Promise.all(queries); },
  };
  await store.migrate();
  const once = [...calls]; calls.length = 0;
  await store.migrate();
  assert.deepEqual(calls, once);
  assert.ok(calls.some((query) => query.includes("trials_device_pubkey_unique")));
});

test("pending scan limits apply in MemoryStore and parameterized Neon queries", async () => {
  const store = new MemoryStore();
  await store.atomic(Array.from({ length: 501 }, (_, i): Op => ({ kind: "insert", table: "claims", row: { id: String(i), status: "pending_verification" } })));
  assert.equal((await store.list("claims", { status: "pending_verification" }, 500)).length, 500);
  const queries: Array<{ query: string; params: unknown[] }> = [];
  const neon = new NeonStore("postgres://local:local@localhost/test");
  (neon as unknown as { sql: unknown }).sql = { async query(query: string, params: unknown[]) { queries.push({ query, params }); return []; } };
  await neon.list("claims", { status: "pending_verification" }, 500);
  assert.match(queries[0].query, /LIMIT \$2$/);
  assert.deepEqual(queries[0].params, ["pending_verification", 500]);
});

test("invalid launch overrides fail startup with a safe stderr diagnostic", async () => {
  const { spawnSync } = await import("node:child_process");
  for (const [setting, provider] of [["GATE_X_LAUNCH_POST_ID", "x"], ["GATE_LINKEDIN_LAUNCH_POST_URL", "linkedin"]]) {
    const result = spawnSync(process.execPath, ["--import", "./test/network-guard.ts", "src/server.ts"], {
      cwd: new URL("..", import.meta.url), encoding: "utf8",
      env: { DEV: "1", [setting]: "do-not-echo-invalid-input" },
    });
    assert.equal(result.status, 1);
    assert.equal(result.stdout, "");
    assert.equal(result.stderr, `ACU gate startup failed: Invalid ${provider} launch post configuration\n`);
    assert.doesNotMatch(result.stderr, /do-not-echo-invalid-input/);
  }
});
