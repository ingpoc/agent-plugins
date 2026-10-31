import { GateError } from "./errors.ts";
import { createHash } from "node:crypto";

export type Provider = "x" | "linkedin";

/** normalizeHandle lowercases X handle without @; LinkedIn slug from URL or slug. */
export function normalizeHandle(provider: Provider, raw: string): string {
  if (provider === "x") {
    const handle = raw.startsWith("@") ? raw.slice(1) : raw;
    if (!/^[A-Za-z0-9_]{1,15}$/.test(handle))
      throw new GateError(400, "invalid_handle");
    return handle.toLowerCase();
  }
  const match = raw.match(
    /^https:\/\/(www\.)?linkedin\.com\/in\/([A-Za-z0-9_-]+)\/?$/i,
  );
  const slug = match ? match[2] : raw;
  if (!/^[A-Za-z0-9_-]{1,100}$/.test(slug))
    throw new GateError(400, "invalid_handle");
  return slug.toLowerCase();
}

/** parseXStatusUrl returns handle, status id, and canonical post key. */
export function parseXStatusUrl(postUrl: string) {
  let url: URL;
  try {
    url = new URL(postUrl);
  } catch {
    throw new GateError(400, "post_url_invalid");
  }
  if (
    url.protocol !== "https:" ||
    !["x.com", "twitter.com", "www.twitter.com"].includes(url.hostname) ||
    url.username ||
    url.password ||
    url.port
  )
    throw new GateError(400, "post_url_invalid");
  const parts = url.pathname.split("/").filter(Boolean);
  if (parts.length < 3 || parts[1] !== "status" || !/^\d+$/.test(parts[2]))
    throw new GateError(400, "post_url_invalid");
  const handle = parts[0].replace(/^@/, "").toLowerCase();
  if (!/^[a-z0-9_]{1,15}$/.test(handle))
    throw new GateError(400, "post_url_invalid");
  return { handle, statusId: parts[2], postKey: `x:${parts[2]}` };
}

/** snowflakeMs converts X status id to creation time in ms. */
export function snowflakeMs(statusId: string) {
  return Number((BigInt(statusId) >> 22n) + 1288834974657n);
}

/** linkedInPostKey normalizes LinkedIn post URLs for used_posts. */
export function linkedInPostKey(postUrl: string) {
  let url: URL;
  try {
    url = new URL(postUrl);
  } catch {
    throw new GateError(400, "post_url_invalid");
  }
  if (
    url.protocol !== "https:" ||
    url.hostname !== "www.linkedin.com" ||
    url.username ||
    url.password ||
    url.port ||
    !url.pathname.startsWith("/")
  )
    throw new GateError(400, "post_url_invalid");
  return `li:${url.pathname.split("?")[0]}`;
}

/** validateLinkedInPostUrl SSRF-safe LinkedIn post URL check. */
export function validateLinkedInPostUrl(postUrl: string) {
  let url: URL;
  try {
    url = new URL(postUrl);
  } catch {
    throw new GateError(400, "post_url_invalid");
  }
  if (
    url.protocol !== "https:" ||
    url.hostname !== "www.linkedin.com" ||
    url.username ||
    url.password ||
    url.port
  )
    throw new GateError(400, "post_url_invalid");
  if (!/^\/(posts|feed\/update)\//.test(url.pathname))
    throw new GateError(400, "post_url_invalid");
  return linkedInPostKey(postUrl);
}

export function linkedInHandleFromPostUrl(postUrl: string) {
  validateLinkedInPostUrl(postUrl);
  const path = new URL(postUrl).pathname;
  const vanity = path.match(/^\/posts\/([A-Za-z0-9-]+)_/i)?.[1];
  if (vanity) return vanity.toLowerCase();
  const activity = path.match(/^\/feed\/update\/urn:li:activity:(\d+)\/?$/i)?.[1] ?? path.match(/activity[-:](\d+)/i)?.[1];
  if (activity) return `activity:${activity}`;
  if (/^\/posts\/[^/]+\/?$/i.test(path))
    return `post:${createHash("sha256").update(path.toLowerCase()).digest("hex")}`;
  throw new GateError(400, "post_url_invalid");
}

const OEMBED_AUTHOR_HOSTS = new Set(["x.com", "twitter.com", "www.twitter.com"]);

/** authorHandleFromOEmbedUrl extracts handle from author_url path. */
export function authorHandleFromOEmbedUrl(authorUrl: string) {
  try {
    const url = new URL(authorUrl);
    if (
      url.protocol !== "https:" ||
      !OEMBED_AUTHOR_HOSTS.has(url.hostname) ||
      url.username ||
      url.password ||
      url.port
    )
      return "";
    const parts = url.pathname.split("/").filter(Boolean);
    return parts[parts.length - 1]?.replace(/^@/, "").toLowerCase() ?? "";
  } catch {
    return "";
  }
}
