import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { NeonStore, tables } from "../src/store.ts";

test("trial migration is additive and declares account/device uniqueness", async () => {
  const migration = await readFile(new URL("../migrations/002_trials.sql", import.meta.url), "utf8");
  assert.match(migration, /^\s*CREATE TABLE IF NOT EXISTS trials\b/i);
  assert.match(migration, /account_id uuid NOT NULL UNIQUE REFERENCES accounts\(id\)/i);
  assert.match(migration, /device_id text NOT NULL UNIQUE/i);
  assert.match(migration, /claim_id uuid NOT NULL REFERENCES claims\(id\)/i);
  assert.match(migration, /CREATE TABLE IF NOT EXISTS gate_settings\s*\(\s*key text PRIMARY KEY,\s*value text NOT NULL,\s*updated_at timestamptz NOT NULL/i);
  assert.deepEqual(tables.trials, [
    "id", "account_id", "device_id", "device_pubkey", "provider", "handle", "claim_id",
    "started_at", "ends_at", "converted_at", "created_at",
  ]);
  assert.deepEqual(tables.gate_settings, ["key", "value", "updated_at"]);
  assert.doesNotMatch(migration, /\bALTER\s+TABLE\b/i);
});

test("migration wiring can be invoked twice through a mocked SQL client", async () => {
  const calls: string[] = [];
  const sql = {
    query(statement: string) {
      calls.push(statement);
      return Promise.resolve([]);
    },
    async transaction(queries: Promise<unknown>[]) {
      await Promise.all(queries);
      return [];
    },
  };
  const store = new NeonStore("postgres://user:pass@localhost/test", "acu_test_migrations");
  (store as unknown as { sql: typeof sql }).sql = sql;

  await store.migrate();
  const firstPass = [...calls];
  calls.length = 0;
  await store.migrate();
  const secondPass = [...calls];

  assert.equal(firstPass[0], 'CREATE SCHEMA IF NOT EXISTS "acu_test_migrations"');
  assert.equal(secondPass[0], firstPass[0]);
  assert.equal(firstPass.filter((sql) => /CREATE TABLE IF NOT EXISTS trials/i.test(sql)).length, 1);
  assert.equal(secondPass.filter((sql) => /CREATE TABLE IF NOT EXISTS trials/i.test(sql)).length, 1);
  assert.equal(firstPass.filter((sql) => /CREATE TABLE IF NOT EXISTS gate_settings/i.test(sql)).length, 1);
  assert.equal(secondPass.filter((sql) => /CREATE TABLE IF NOT EXISTS gate_settings/i.test(sql)).length, 1);
  assert.deepEqual(secondPass.slice(1), firstPass.slice(1));
});
