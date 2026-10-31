import { test } from "node:test";
import assert from "node:assert/strict";
import { createSign, generateKeyPairSync } from "node:crypto";
import {
  verifyLinkedInIdToken,
} from "../src/providers.ts";
import { startMocks } from "../src/mock-providers.ts";
import { harness } from "./helpers.ts";
import { MemoryStore } from "../src/store.ts";

function signBadLinkedInToken(
  env: NodeJS.ProcessEnv,
  overrides: Record<string, unknown>,
) {
  const keys = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = keys.publicKey.export({ format: "jwk" }) as Record<string, string>;
  jwk.kid = "bad";
  const header = Buffer.from(
    JSON.stringify({ alg: "RS256", typ: "JWT", kid: "bad" }),
  ).toString("base64url");
  const now = Math.floor(Date.now() / 1000);
  const payload = Buffer.from(
    JSON.stringify({
      iss: "https://www.linkedin.com",
      aud: env.LINKEDIN_CLIENT_ID,
      sub: "li:test",
      exp: now + 3600,
      nonce: "expected",
      ...overrides,
    }),
  ).toString("base64url");
  const data = `${header}.${payload}`;
  const sig = createSign("RSA-SHA256")
    .update(data)
    .sign(keys.privateKey)
    .toString("base64url");
  return { token: `${data}.${sig}`, jwk };
}

test("LinkedIn id_token rejects bad nonce, aud, and signature", async (t) => {
  const mock = await startMocks();
  t.after(mock.close);
  const env = {
    ...mock.env,
    LINKEDIN_CLIENT_ID: "test-client",
    LINKEDIN_JWKS_URL: `${mock.base}/oauth/openid/jwks`,
  };
  await assert.rejects(
    verifyLinkedInIdToken(
      signBadLinkedInToken(env, { nonce: "wrong" }).token,
      env,
      "expected",
    ),
    (error: { code?: string }) => error.code === "invalid_token",
  );
  await assert.rejects(
    verifyLinkedInIdToken(
      signBadLinkedInToken(env, { aud: "other-client" }).token,
      env,
      "expected",
    ),
    (error: { code?: string }) => error.code === "invalid_token",
  );
  const mismatched = signBadLinkedInToken(env, {});
  await assert.rejects(
    verifyLinkedInIdToken(mismatched.token, env, "expected"),
    (error: { code?: string }) => error.code === "invalid_token",
  );
});

test("OAuth callback stores token without calling X users/me", async (t) => {
  const h = await harness(t);
  const prepared = await h.authorize("x", "guru_test");
  assert.ok(prepared.code);
  assert.equal((await h.store.list("identities")).length, 0);
  assert.equal((await h.store.list("x_calls")).length, 0);
  const redeemed = await h.request("/v1/session/redeem", {
    data: {
      code: prepared.code,
      app_verifier: prepared.verifier,
      device_id: prepared.device.id,
      device_pubkey: prepared.device.key,
    },
  });
  assert.equal(redeemed.status, 200);
});

test("verify rate limit returns retry_after", async (t) => {
  const h = await harness(t);
  const a = await h.login();
  const first = await h.request("/v1/verify/x", {
    session: a.data.session,
    data: {},
  });
  assert.notEqual(first.status, 429);
  const second = await h.request("/v1/verify/x", {
    session: a.data.session,
    data: {},
  });
  assert.equal(second.status, 429);
  assert.equal(second.data.error, "rate_limited");
  assert.equal(typeof second.data.retry_after, "number");
});

test("X daily budget circuit breaker", async (t) => {
  const h = await harness(t, new MemoryStore(), { X_DAILY_CALL_BUDGET: "1" });
  const a = await h.login();
  const result = await h.request("/v1/verify/x", {
    session: a.data.session,
    data: {},
  });
  assert.equal(result.status, 503);
  assert.equal(result.data.error, "x_unavailable");
});
