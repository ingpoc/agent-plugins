import { test } from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync, sign, verify } from "node:crypto";
import {
  issueEntitlement,
  ReplayGuard,
  verifyDeviceSignature,
} from "../src/crypto.ts";

test("entitlement has a verifiable Ed25519 signature and fourteen-day expiry", () => {
  const keys = generateKeyPairSync("ed25519");
  const privateKey = keys.privateKey
    .export({ format: "der", type: "pkcs8" })
    .toString("base64");
  const [payload, signature] = issueEntitlement(
    privateKey,
    { acct: "a", prov: "x", handle: "h", device_id: "a".repeat(64) },
    1000,
  ).split(".");
  assert.equal(
    verify(
      null,
      Buffer.from(payload),
      keys.publicKey,
      Buffer.from(signature, "base64url"),
    ),
    true,
  );
  const decoded = JSON.parse(Buffer.from(payload, "base64url").toString());
  assert.equal(decoded.exp - decoded.iat, 14 * 86400);
});

test("device signatures bind action, device and timestamp", () => {
  const keys = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const jwk = keys.publicKey.export({ format: "jwk" });
  const raw = Buffer.concat([
    Buffer.from([4]),
    Buffer.from(jwk.x!, "base64url"),
    Buffer.from(jwk.y!, "base64url"),
  ]).toString("base64");
  const id = "a".repeat(64);
  const signature = sign(
    "sha256",
    Buffer.from(`acu-gate-refresh|${id}|1000`),
    keys.privateKey,
  ).toString("base64");
  assert.equal(
    verifyDeviceSignature(raw, "refresh", id, 1000, signature),
    true,
  );
  assert.equal(
    verifyDeviceSignature(raw, "rebind", id, 1000, signature),
    false,
  );
  assert.equal(
    verifyDeviceSignature(raw, "refresh", id, 1001, signature),
    false,
  );
});

test("replay guard rejects stale timestamps and repeated signatures", () => {
  const guard = new ReplayGuard();
  assert.equal(guard.accept("device", 1000, 1000), true);
  assert.equal(guard.accept("device", 1000, 1000), false);
  assert.equal(guard.accept("device", 699, 1000), false);
  assert.equal(guard.accept("device", 1301, 1000), false);
});

test("wall signatures bind the consent value", () => {
  const keys = generateKeyPairSync("ec", { namedCurve: "prime256v1" }),
    jwk = keys.publicKey.export({ format: "jwk" });
  const raw = Buffer.concat([
    Buffer.from([4]),
    Buffer.from(jwk.x!, "base64url"),
    Buffer.from(jwk.y!, "base64url"),
  ]).toString("base64");
  const id = "b".repeat(64);
  const signature = sign(
    "sha256",
    Buffer.from(`acu-gate-wall|${id}|1000|false`),
    keys.privateKey,
  ).toString("base64");
  assert.equal(
    verifyDeviceSignature(raw, "wall", id, 1000, signature, false),
    true,
  );
  assert.equal(
    verifyDeviceSignature(raw, "wall", id, 1000, signature, true),
    false,
  );
});
