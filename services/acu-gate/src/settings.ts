import {
  createPrivateKey,
  generateKeyPairSync,
  randomBytes,
  createCipheriv,
  createDecipheriv,
  createHmac,
  timingSafeEqual,
} from "node:crypto";
import { readFileSync } from "node:fs";
export const preset = JSON.parse(
  readFileSync(new URL("../config/preset.json", import.meta.url), "utf8"),
);
/** config provides its exported service behavior. */
export function config() {
  return {
    x: {
      ...preset.x,
      intent_url: `https://x.com/intent/post?text=${encodeURIComponent(preset.x.preset_text)}`,
    },
    linkedin: {
      ...preset.linkedin,
      share_url: `https://www.linkedin.com/sharing/share-offsite/?url=${encodeURIComponent(
        preset.match.ref_links[1],
      )}`,
      prefill_text: preset.linkedin_prefill_text,
    },
    match: preset.match,
    providers: ["x", "linkedin"],
    grace_days: 14,
  };
}
/** settings provides its exported service behavior. */
export function settings(input: NodeJS.ProcessEnv) {
  const env = { ...input };
  if (env.DEV === "1") {
    env.ENTITLEMENT_PRIVATE_KEY ||= generateKeyPairSync("ed25519")
      .privateKey.export({ format: "der", type: "pkcs8" })
      .toString("base64");
    for (const key of ["TOKEN_ENC_KEY", "EMAIL_HASH_KEY"])
      env[key] ||= randomBytes(32).toString("base64");
    for (const key of [
      "X_CLIENT_ID",
      "X_CLIENT_SECRET",
      "LINKEDIN_CLIENT_ID",
      "LINKEDIN_CLIENT_SECRET",
      "ADMIN_TOKEN",
    ])
      env[key] ||= randomBytes(24).toString("base64url");
    env.OWNER_X_USER_ID ||= "owner";
    env.PUBLIC_BASE_URL ||= "http://127.0.0.1:8790";
  }
  for (const key of [
    "PUBLIC_BASE_URL",
    "X_CLIENT_ID",
    "X_CLIENT_SECRET",
    "OWNER_X_USER_ID",
    "LINKEDIN_CLIENT_ID",
    "LINKEDIN_CLIENT_SECRET",
    "ENTITLEMENT_PRIVATE_KEY",
    "TOKEN_ENC_KEY",
    "EMAIL_HASH_KEY",
    "ADMIN_TOKEN",
  ])
    if (!env[key]) throw new Error(`Missing required setting: ${key}`);
  const base = new URL(env.PUBLIC_BASE_URL!);
  if (
    base.username ||
    base.password ||
    base.search ||
    base.hash ||
    base.pathname !== "/" ||
    (env.DEV !== "1" && base.protocol !== "https:") ||
    !["http:", "https:"].includes(base.protocol)
  )
    throw new Error("Invalid PUBLIC_BASE_URL");
  env.PUBLIC_BASE_URL = base.origin;
  if (env.DEV !== "1" && !env.DATABASE_URL && !env.ACU_GATE_DATABASE_URL_FILE)
    throw new Error("Missing required setting: DATABASE_URL");
  if (
    Buffer.from(env.TOKEN_ENC_KEY!, "base64").length !== 32 ||
    Buffer.from(env.EMAIL_HASH_KEY!, "base64").length !== 32
  )
    throw new Error("Encryption and email keys must be base64 32-byte values");
  if (
    createPrivateKey({
      key: Buffer.from(env.ENTITLEMENT_PRIVATE_KEY!, "base64"),
      format: "der",
      type: "pkcs8",
    }).asymmetricKeyType !== "ed25519"
  )
    throw new Error("ENTITLEMENT_PRIVATE_KEY must be Ed25519 PKCS8 DER");
  env.APP_CALLBACK_SCHEME ||= "acu-island";
  if (!/^[a-z][a-z0-9+.-]*$/.test(env.APP_CALLBACK_SCHEME))
    throw new Error("Invalid callback scheme");
  if (
    !["owner_followers", "user_following"].includes(
      env.FOLLOW_CHECK_MODE || "owner_followers",
    )
  )
    throw new Error("Invalid FOLLOW_CHECK_MODE");
  const hops = Number(env.TRUST_PROXY_HOPS ?? 0);
  if (!Number.isInteger(hops) || hops < 0 || hops > 8)
    throw new Error("Invalid TRUST_PROXY_HOPS");
  env.TRUST_PROXY_HOPS = String(hops);
  const budget = Number(env.X_DAILY_CALL_BUDGET ?? 2000);
  if (!Number.isInteger(budget) || budget < 1)
    throw new Error("Invalid X_DAILY_CALL_BUDGET");
  env.X_DAILY_CALL_BUDGET = String(budget);
  env.LINKEDIN_JWKS_URL ||=
    "https://www.linkedin.com/oauth/openid/jwks";
  return env;
}
/** encrypt provides its exported service behavior. */
export function encrypt(value: string, key: string) {
  const nonce = randomBytes(12);
  const cipher = createCipheriv(
    "aes-256-gcm",
    Buffer.from(key, "base64"),
    nonce,
  );
  const ciphertext = Buffer.concat([
    cipher.update(value, "utf8"),
    cipher.final(),
  ]);
  return {
    ciphertext: ciphertext.toString("base64"),
    nonce: nonce.toString("base64"),
    tag: cipher.getAuthTag().toString("base64"),
  };
}
/** decrypt provides its exported service behavior. */
export function decrypt(
  value: { ciphertext: string; nonce: string; tag: string },
  key: string,
) {
  const cipher = createDecipheriv(
    "aes-256-gcm",
    Buffer.from(key, "base64"),
    Buffer.from(value.nonce, "base64"),
  );
  cipher.setAuthTag(Buffer.from(value.tag, "base64"));
  return Buffer.concat([
    cipher.update(Buffer.from(value.ciphertext, "base64")),
    cipher.final(),
  ]).toString("utf8");
}
/** tokenBlob provides its exported service behavior. */
export function tokenBlob(value: string, key: string) {
  return JSON.stringify(encrypt(value, key));
}
/** openBlob provides its exported service behavior. */
export function openBlob(value: string, key: string) {
  return decrypt(JSON.parse(value), key);
}
/** emailHash provides its exported service behavior. */
export function emailHash(email: string, key: string) {
  return createHmac("sha256", Buffer.from(key, "base64"))
    .update(email.trim().toLowerCase())
    .digest("hex");
}
/** constantEqual provides its exported service behavior. */
export function constantEqual(a: string, b: string) {
  const aa = Buffer.from(a),
    bb = Buffer.from(b);
  return aa.length === bb.length && timingSafeEqual(aa, bb);
}
