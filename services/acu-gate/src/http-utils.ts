import { object } from "./errors.ts";

/** limitedBody reads a response body up to maxBytes. */
export async function limitedBody(response: Response, maxBytes = 256 * 1024) {
  const reader = response.body?.getReader();
  if (!reader) return "";
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > maxBytes) throw new Error("body_too_large");
    chunks.push(value);
  }
  return Buffer.concat(chunks).toString("utf8");
}

/** decodeHtmlEntities strips tags and decodes common entities from oEmbed html. */
export function htmlToText(html: string) {
  const stripped = html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return stripped
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, " ");
}

/** parseOEmbedJson validates oEmbed response shape. */
export function parseOEmbedJson(raw: string) {
  const data = object(JSON.parse(raw));
  if (typeof data.author_url !== "string" || typeof data.html !== "string")
    throw new Error("invalid_oembed");
  return {
    author_url: data.author_url,
    author_name:
      typeof data.author_name === "string" ? data.author_name : "",
    html: data.html,
  };
}
