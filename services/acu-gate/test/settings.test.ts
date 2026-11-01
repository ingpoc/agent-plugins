import { test } from "node:test";
import assert from "node:assert/strict";
import { settings, config, matchesPreset } from "../src/settings.ts";
import { issueEntitlement } from "../src/crypto.ts";
import { normalizeHandle } from "../src/handles.ts";

test("settings requires production secrets", () => {
  assert.throws(() => settings({ PUBLIC_BASE_URL: "https://x.test" }), /ENTITLEMENT_PRIVATE_KEY/);
});

test("config exposes poll interval and presets", () => {
  const value = config();
  assert.equal(value.poll_seconds, 60);
  assert.equal(value.check_eta_text, "usually within the hour");
  assert.match(value.x.intent_url, /intent\/post/);
  const follow = new URL(value.x.follow_url);
  assert.equal(follow.origin, "https://x.com");
  assert.equal(follow.pathname, `/${value.x.handle}`);
  assert.equal(follow.search, "");
  const share = new URL(value.linkedin.share_url);
  assert.equal(share.pathname, "/feed/");
  assert.equal(share.searchParams.get("shareActive"), "true");
  assert.equal(share.searchParams.get("text"), value.linkedin.preset_text);
  assert.equal(value.grace_days, 14);
  assert.equal(value.x.launch_post_id, null);
  assert.equal(value.x.repost_url, null);
  assert.equal(value.linkedin.launch_post_url, null);
  assert.equal(value.linkedin.repost_url, null);
});

test("preset matching supports provider-specific references from both domains", () => {
  assert.equal(
    matchesPreset("https://ingpoc-agent-plugins.rpg-ramashram.chatgpt.site/?ref=acu-x", "x"),
    true,
  );
  assert.equal(
    matchesPreset("https://ingpoc-agent-plugins.nicewasp128.chatgpt.site/?ref=acu-li", "linkedin"),
    true,
  );
  assert.equal(
    matchesPreset("https://ingpoc-agent-plugins.rpg-ramashram.chatgpt.site/?ref=acu-li", "x"),
    false,
  );
});

test("handle normalization accepts LinkedIn vanity and profile URLs with 3 to 100 characters", () => {
  assert.equal(normalizeHandle("linkedin", "Neon-User"), "neon-user");
  assert.equal(normalizeHandle("linkedin", "https://www.linkedin.com/in/Neon-User/"), "neon-user");
  assert.equal(normalizeHandle("linkedin", `https://in.linkedin.com/in/${"a".repeat(100)}`), "a".repeat(100));
  assert.throws(() => normalizeHandle("linkedin", "ab"), /invalid handle/);
  assert.throws(() => normalizeHandle("linkedin", "a".repeat(101)), /invalid handle/);
  assert.equal(normalizeHandle("x", "@Neon_User"), "neon_user");
});

test("trial entitlements carry trial tier and fixed expiry; regular entitlements retain grace", () => {
  const key = settings({ DEV: "1", PUBLIC_BASE_URL: "http://127.0.0.1:1" }).ENTITLEMENT_PRIVATE_KEY!;
  const now = 1_800_000_000;
  const decode = (token: string) => JSON.parse(Buffer.from(token.split(".")[0], "base64url").toString("utf8"));
  const trial = decode(issueEntitlement(key, {
    acct: "acct", prov: "linkedin", handle: "fan", device_id: "device",
    tier: "trial", exp: now + 72 * 3600,
  }, now));
  assert.equal(trial.tier, "trial");
  assert.equal(trial.exp, now + 72 * 3600);
  assert.equal(trial.iat, now);
  const regular = decode(issueEntitlement(key, {
    acct: "acct", prov: "x", handle: "fan", device_id: "device",
  }, now));
  assert.equal(regular.tier, undefined);
  assert.equal(regular.exp, now + 14 * 86400);
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
