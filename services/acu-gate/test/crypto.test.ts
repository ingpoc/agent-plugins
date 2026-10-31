import { test } from "node:test";
import assert from "node:assert/strict";
import { sign } from "node:crypto";
import { deviceMessage, verifyDeviceSignature, sha256Hex } from "../src/crypto.ts";
import { device } from "./helpers.ts";

test("device signature covers body hash", () => {
  const d = device();
  const body = '{"device_id":"abc"}';
  const ts = Math.floor(Date.now() / 1000);
  const hash = sha256Hex(body);
  const message = deviceMessage("refresh", d.id, ts, hash);
  const sig = sign("sha256", Buffer.from(message), d.privateKey).toString("base64");
  assert.equal(verifyDeviceSignature(d.key, "refresh", d.id, ts, sig, hash), true);
  assert.equal(verifyDeviceSignature(d.key, "refresh", d.id, ts, sig, sha256Hex("{}")), false);
});
