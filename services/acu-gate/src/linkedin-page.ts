import { GateError } from "./errors.ts";
import { limitedBody } from "./http-utils.ts";
import { preset } from "./settings.ts";
import { linkedInPostKey } from "./handles.ts";

export type PageFetch = (
  url: string,
) => Promise<{ status: number; body: string }>;

function matchesText(text: string) {
  const ref = preset.match.ref_links[1];
  return (
    text.toLowerCase().includes(preset.match.hashtag.toLowerCase()) ||
    text.includes(ref)
  );
}

/** fetchLinkedInPublicPost tries one bounded public fetch for auto-pass. */
export async function fetchLinkedInPublicPost(
  postUrl: string,
  profileSlug: string,
  pageFetch: PageFetch,
) {
  const key = linkedInPostKey(postUrl);
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
  const result = await pageFetch(postUrl);
  if (result.status !== 200) return { ok: false as const, postKey: key };
  const body = result.body.slice(0, 512 * 1024);
  if (!matchesText(body)) return { ok: false as const, postKey: key };
  const slugPattern = `/in/${profileSlug}`;
  const authorLink =
    body.includes(`href="${slugPattern}"`) ||
    body.includes(`href='${slugPattern}'`) ||
    body.includes(`linkedin.com/in/${profileSlug}`);
  if (!authorLink) return { ok: false as const, postKey: key };
  return { ok: true as const, postKey: key, text: body };
}

/** verifyLinkedInCometPost checks admin comet evidence text. */
export function verifyLinkedInCometPost(postText: string) {
  if (!matchesText(postText)) throw new GateError(422, "post_missing_tag");
}

/** defaultPageFetch production fetch with SSRF bounds. */
export async function defaultPageFetch(url: string) {
  const parsed = new URL(url);
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname !== "www.linkedin.com" ||
    parsed.username ||
    parsed.password ||
    parsed.port
  )
    throw new GateError(400, "post_url_invalid");
  const response = await fetch(parsed, {
    signal: AbortSignal.timeout(8000),
    redirect: "error",
    headers: { accept: "text/html" },
  });
  return { status: response.status, body: await limitedBody(response, 512 * 1024) };
}
