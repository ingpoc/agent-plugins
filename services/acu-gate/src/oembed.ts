import { GateError } from "./errors.ts";
import { htmlToText, limitedBody, parseOEmbedJson } from "./http-utils.ts";
import {
  authorHandleFromOEmbedUrl,
  parseXStatusUrl,
  snowflakeMs,
} from "./handles.ts";
import { oEmbedHosts, preset } from "./settings.ts";

export type OEmbedResult =
  | {
      ok: true;
      postKey: string;
      text: string;
      authorHandle: string;
      createdMs: number;
    }
  | { ok: false; code: "post_not_found" | "post_author_mismatch" | "post_too_old" | "post_missing_tag" };

export type OEmbedClient = {
  fetch(postUrl: string, claimHandle: string, claimCreatedMs: number, minCreatedMs?: number): Promise<OEmbedResult>;
};

function matchesText(text: string, provider: "x" | "linkedin") {
  const ref = preset.match.ref_links[provider === "x" ? 0 : 1];
  return (
    text.toLowerCase().includes(preset.match.hashtag.toLowerCase()) ||
    text.includes(ref)
  );
}

/** createOEmbedClient builds the production oEmbed client. */
export function createOEmbedClient(env: NodeJS.ProcessEnv): OEmbedClient {
  const base = env.OEMBED_BASE || "https://publish.x.com/oembed";
  const allowedHosts = oEmbedHosts(env);
  return {
    async fetch(postUrl, claimHandle, claimCreatedMs, minCreatedMs = 0) {
      const { handle, statusId, postKey } = parseXStatusUrl(postUrl);
      if (handle !== claimHandle)
        return { ok: false, code: "post_author_mismatch" };
      const createdMs = snowflakeMs(statusId);
      const floor = Math.max(claimCreatedMs - 24 * 3600000, minCreatedMs);
      if (createdMs < floor) return { ok: false, code: "post_too_old" };
      const target = new URL(base);
      target.searchParams.set("url", postUrl);
      target.searchParams.set("omit_script", "1");
      target.searchParams.set("dnt", "true");
      let response: Response;
      try {
        response = await fetch(target, {
          signal: AbortSignal.timeout(8000),
          redirect: "follow",
        });
      } catch {
        throw new GateError(503, "server_error");
      }
      if (response.status === 404) return { ok: false, code: "post_not_found" };
      if (response.status === 403) return { ok: false, code: "post_not_found" };
      if (!response.ok) throw new GateError(503, "server_error");
      const responseHost = new URL(response.url).hostname;
      if (!allowedHosts.has(responseHost)) throw new GateError(503, "server_error");
      const parsed = parseOEmbedJson(await limitedBody(response));
      const authorHandle = authorHandleFromOEmbedUrl(parsed.author_url);
      if (authorHandle !== claimHandle)
        return { ok: false, code: "post_author_mismatch" };
      const text = htmlToText(parsed.html);
      if (!matchesText(text, "x"))
        return { ok: false, code: "post_missing_tag" };
      return { ok: true, postKey, text, authorHandle, createdMs };
    },
  };
}

/** classifyOEmbedHttpStatus maps fetch status for comet fallback rules. */
export function classifyOEmbedHttpStatus(status: number) {
  return status === 404 || status === 403;
}
