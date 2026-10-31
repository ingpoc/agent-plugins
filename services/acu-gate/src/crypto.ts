import { createHash, createPublicKey, sign, verify } from "node:crypto";

/** sha256 provides its exported service behavior. */
export function sha256(value: string): string {
  return createHash("sha256").update(value).digest("base64url");
}

/** issueEntitlement provides its exported service behavior. */
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

/** verifyDeviceSignature provides its exported service behavior. */
export function verifyDeviceSignature(
  publicKey: string,
  action: "refresh" | "rebind" | "wall",
  deviceId: string,
  ts: number,
  signature: string,
  show?: boolean,
): boolean {
  try {
    const raw = Buffer.from(publicKey, "base64");
    if (
      raw.length !== 65 ||
      raw[0] !== 4 ||
      !/^[a-f0-9]{64}$/.test(deviceId) ||
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
      Buffer.from(
        `acu-gate-${action}|${deviceId}|${ts}${action === "wall" ? `|${show}` : ""}`,
      ),
      key,
      Buffer.from(signature, "base64"),
    );
  } catch {
    return false;
  }
}

export class ReplayGuard {
  private seen = new Map<string, number>();

  accept(
    deviceId: string,
    ts: number,
    now = Math.floor(Date.now() / 1000),
  ): boolean {
    if (!Number.isSafeInteger(ts) || Math.abs(now - ts) > 300) return false;
    for (const [key, expires] of this.seen)
      if (expires <= now) this.seen.delete(key);
    const key = `${deviceId}|${ts}`;
    if (this.seen.has(key)) return false;
    this.seen.set(key, now + 600);
    return true;
  }
}

/** safeAvatar provides its exported service behavior. */
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
