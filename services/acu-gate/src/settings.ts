import {
  createPrivateKey,
  generateKeyPairSync,
  randomBytes,
  timingSafeEqual,
} from "node:crypto";
import { readFileSync } from "node:fs";
export const preset = JSON.parse(
  readFileSync(new URL("../config/preset.json", import.meta.url), "utf8"),
);
/** config provides public app configuration. */
export function config() {
  return {
    x: {
      ...preset.x,
      profile_url: `https://x.com/${preset.x.handle}`,
      intent_url: `https://x.com/intent/post?text=${encodeURIComponent(preset.x.preset_text)}`,
      preset_text: preset.x.preset_text,
    },
    linkedin: {
      ...preset.linkedin,
      share_url: `https://www.linkedin.com/sharing/share-offsite/?url=${encodeURIComponent(
        preset.match.ref_links[1],
      )}`,
      preset_text: preset.linkedin.preset_text,
    },
    match: preset.match,
    grace_days: 14,
    poll_seconds: 60,
    check_eta_text: preset.check_eta_text,
  };
}
/** settings validates environment configuration. */
export function settings(input: NodeJS.ProcessEnv) {
  const env = { ...input };
  if (env.DEV === "1") {
    env.ENTITLEMENT_PRIVATE_KEY ||= generateKeyPairSync("ed25519")
      .privateKey.export({ format: "der", type: "pkcs8" })
      .toString("base64");
    env.ADMIN_TOKEN ||= randomBytes(24).toString("base64url");
    env.PUBLIC_BASE_URL ||= "http://127.0.0.1:8790";
  }
  for (const key of ["PUBLIC_BASE_URL", "ENTITLEMENT_PRIVATE_KEY", "ADMIN_TOKEN"])
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
    createPrivateKey({
      key: Buffer.from(env.ENTITLEMENT_PRIVATE_KEY!, "base64"),
      format: "der",
      type: "pkcs8",
    }).asymmetricKeyType !== "ed25519"
  )
    throw new Error("ENTITLEMENT_PRIVATE_KEY must be Ed25519 PKCS8 DER");
  const hops = Number(env.TRUST_PROXY_HOPS ?? 0);
  if (!Number.isInteger(hops) || hops < 0 || hops > 8)
    throw new Error("Invalid TRUST_PROXY_HOPS");
  env.TRUST_PROXY_HOPS = String(hops);
  env.OEMBED_BASE = normalizeOEmbedBase(env.OEMBED_BASE, env.DEV === "1");
  return env;
}
const PRODUCTION_OEMBED_HOSTS = new Set(["publish.x.com", "publish.twitter.com"]);

/** oEmbedHosts returns hostnames allowed for oEmbed fetch redirects. */
export function oEmbedHosts(env: NodeJS.ProcessEnv) {
  const hosts = new Set(PRODUCTION_OEMBED_HOSTS);
  if (env.DEV === "1") {
    try {
      if (new URL(env.OEMBED_BASE!).hostname === "127.0.0.1")
        hosts.add("127.0.0.1");
    } catch {
      /* settings already validated OEMBED_BASE */
    }
  }
  return hosts;
}

function normalizeOEmbedBase(raw: string | undefined, dev: boolean) {
  const url = new URL(raw || "https://publish.x.com/oembed");
  if (
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname !== "/oembed"
  )
    throw new Error("Invalid OEMBED_BASE");
  if (dev && url.hostname === "127.0.0.1" && url.protocol === "http:")
    return url.href.replace(/\/$/, "");
  if (url.protocol !== "https:" || !PRODUCTION_OEMBED_HOSTS.has(url.hostname))
    throw new Error("Invalid OEMBED_BASE");
  return `${url.origin}/oembed`;
}
/** constantEqual compares strings in constant time. */
export function constantEqual(a: string, b: string) {
  const aa = Buffer.from(a),
    bb = Buffer.from(b);
  return aa.length === bb.length && timingSafeEqual(aa, bb);
}
