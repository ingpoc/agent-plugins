import { test } from "node:test";
import assert from "node:assert/strict";
import { settings, config } from "../src/settings.ts";

test("settings requires production secrets", () => {
  assert.throws(() => settings({ PUBLIC_BASE_URL: "https://x.test" }), /ENTITLEMENT_PRIVATE_KEY/);
});

test("config exposes poll interval and presets", () => {
  const value = config();
  assert.equal(value.poll_seconds, 60);
  assert.equal(value.check_eta_text, "usually within an hour");
  assert.match(value.x.intent_url, /intent\/post/);
  assert.equal(value.grace_days, 14);
});

test("dev settings synthesize admin token", () => {
  const env = settings({ DEV: "1", PUBLIC_BASE_URL: "http://127.0.0.1:1" });
  assert.ok(env.ADMIN_TOKEN);
  assert.ok(env.ENTITLEMENT_PRIVATE_KEY);
  assert.equal(env.OEMBED_BASE, "https://publish.x.com/oembed");
});

test("production rejects unsafe OEMBED_BASE", () => {
  const key = settings({
    DEV: "1",
    PUBLIC_BASE_URL: "http://127.0.0.1:1",
  }).ENTITLEMENT_PRIVATE_KEY;
  assert.throws(
    () =>
      settings({
        PUBLIC_BASE_URL: "https://gate.test",
        ENTITLEMENT_PRIVATE_KEY: key,
        ADMIN_TOKEN: "x".repeat(32),
        DATABASE_URL: "postgres://local/test",
        OEMBED_BASE: "http://169.254.169.254/oembed",
      }),
    /Invalid OEMBED_BASE/,
  );
});
