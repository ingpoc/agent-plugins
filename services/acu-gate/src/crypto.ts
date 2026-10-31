import {
  createHash,
  createPublicKey,
  sign,
  verify,
} from "node:crypto";

/** sha256Hex returns hex SHA-256 of UTF-8 input. */
export function sha256Hex(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

/** issueEntitlement signs a 14-day offline grace entitlement. */
export function issueEntitlement(
  privateKey: string,
  claims: {
    acct: string;
    prov: "x" | "linkedin";
    handle: string;
    device_id: string;
  },
  now = Math.floor(Date.now() / 1000),
): string {
  const payload = Buffer.from(
    JSON.stringify({ v: 1, ...claims, iat: now, exp: now + 14 * 86400 }),
  ).toString("base64url");
  const signature = sign(null, Buffer.from(payload), {
    key: Buffer.from(privateKey, "base64"),
    format: "der",
    type: "pkcs8",
  });
  return `${payload}.${signature.toString("base64url")}`;
}

export type DeviceAction = "claim" | "poll" | "refresh" | "wall";

/** deviceMessage builds the signed payload string. */
export function deviceMessage(
  action: DeviceAction,
  deviceId: string,
  ts: number,
  bodySha256Hex: string,
) {
  return `acu-gate|${action}|${deviceId}|${ts}|${bodySha256Hex}`;
}

/** verifyDeviceSignature validates P-256 ECDSA over the device message. */
export function verifyDeviceSignature(
  publicKey: string,
  action: DeviceAction,
  deviceId: string,
  ts: number,
  signature: string,
  bodySha256Hex: string,
): boolean {
  try {
    const raw = Buffer.from(publicKey, "base64");
    if (
      raw.length !== 65 ||
      raw[0] !== 4 ||
      !/^[a-f0-9]{64}$/.test(deviceId) ||
      !/^[a-f0-9]{64}$/.test(bodySha256Hex) ||
      !Number.isSafeInteger(ts)
    )
      return false;
    const key = createPublicKey({
      key: {
        kty: "EC",
        crv: "P-256",
        x: raw.subarray(1, 33).toString("base64url"),
        y: raw.subarray(33).toString("base64url"),
      },
      format: "jwk",
    });
    return verify(
      "sha256",
      Buffer.from(deviceMessage(action, deviceId, ts, bodySha256Hex)),
      key,
      Buffer.from(signature, "base64"),
    );
  } catch {
    return false;
  }
}

/** safeAvatar allows only approved avatar hosts. */
export function safeAvatar(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" &&
      ["pbs.twimg.com", "media.licdn.com"].includes(url.hostname) &&
      !url.username &&
      !url.password &&
      !url.port
      ? url.href
      : null;
  } catch {
    return null;
  }
}
