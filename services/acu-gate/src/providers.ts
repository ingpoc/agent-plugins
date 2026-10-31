import { createHash, randomBytes, createPublicKey, verify } from "node:crypto";
import { GateError, object, text } from "./errors.ts";
import { safeAvatar } from "./crypto.ts";
import type { Store } from "./store.ts";
export type ProviderName = "x" | "linkedin";
export type Identity = {
  userId: string;
  handle: string;
  name: string;
  avatar: string | null;
  profile: string | null;
  email?: string;
  verified?: boolean;
  verifiedType?: string;
  followers?: number;
};
export interface Provider {
  authorize(
    state: string,
    verifier: string,
    callback: string,
    nonce?: string,
    owner?: boolean,
  ): URL;
  exchange(code: string, verifier: string, callback: string, nonce?: string): Promise<string>;
  identity(token: string): Promise<Identity>;
}
export type XTokenBundle = {
  accessToken: string;
  refreshToken?: string;
  expiresAt: string;
};
const X_USER_SCOPES = "tweet.read users.read follows.read";
const X_OWNER_SCOPES = `${X_USER_SCOPES} offline.access`;
export const pkce = (s: string) =>
  createHash("sha256").update(s).digest("base64url");
export const opaque = () => randomBytes(32).toString("base64url");
/** limitedBody provides its exported service behavior. */
export async function limitedBody(response: Response, max = 2 * 1024 * 1024) {
  if (Number(response.headers.get("content-length")) > max)
    throw new Error("body_too_large");
  const chunks: Uint8Array[] = [];
  let length = 0;
  if (response.body) {
    const reader = response.body.getReader();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        length += value.length;
        if (length > max) throw new Error("body_too_large");
        chunks.push(value);
      }
    } finally {
      await reader.cancel();
    }
  }
  return Buffer.concat(chunks).toString("utf8");
}
async function json(
  url: string | URL,
  init: RequestInit,
  provider: ProviderName,
) {
  try {
    const response = await fetch(url, {
      ...init,
      signal: AbortSignal.timeout(8000),
    });
    if (!response.ok) throw new Error("provider_failure");
    return object(JSON.parse(await limitedBody(response)));
  } catch {
    throw new GateError(
      503,
      provider === "x" ? "x_unavailable" : "linkedin_unavailable",
    );
  }
}
function list(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(object) : [];
}
export type XSpend = () => Promise<void>;
export class XProvider implements Provider {
  env: NodeJS.ProcessEnv;
  spend: XSpend;
  constructor(env: NodeJS.ProcessEnv, spend: XSpend = async () => {}) {
    this.env = env;
    this.spend = spend;
  }
  authorize(
    state: string,
    verifier: string,
    callback: string,
    _nonce?: string,
    owner?: boolean,
  ) {
    const url = new URL(
      "/i/oauth2/authorize",
      this.env.X_AUTH_BASE || "https://x.com",
    );
    url.search = new URLSearchParams({
      response_type: "code",
      client_id: this.env.X_CLIENT_ID || "",
      redirect_uri: callback,
      state,
      scope: owner ? X_OWNER_SCOPES : X_USER_SCOPES,
      code_challenge: pkce(verifier),
      code_challenge_method: "S256",
    }).toString();
    return url;
  }
  private async tokenRequest(body: URLSearchParams) {
    return json(
      new URL("/2/oauth2/token", this.env.X_API_BASE || "https://api.x.com"),
      {
        method: "POST",
        headers: {
          "content-type": "application/x-www-form-urlencoded",
          authorization: `Basic ${Buffer.from(
            `${this.env.X_CLIENT_ID}:${this.env.X_CLIENT_SECRET}`,
          ).toString("base64")}`,
        },
        body,
      },
      "x",
    );
  }
  private bundleFromTokenResponse(data: Record<string, unknown>): XTokenBundle {
    const expiresIn =
      typeof data.expires_in === "number" && data.expires_in > 0
        ? data.expires_in
        : 7200;
    return {
      accessToken: text(data.access_token),
      refreshToken:
        typeof data.refresh_token === "string" ? data.refresh_token : undefined,
      expiresAt: new Date(Date.now() + expiresIn * 1000).toISOString(),
    };
  }
  async exchangeTokens(code: string, verifier: string, callback: string) {
    return this.bundleFromTokenResponse(
      await this.tokenRequest(
        new URLSearchParams({
          grant_type: "authorization_code",
          code,
          redirect_uri: callback,
          code_verifier: verifier,
        }),
      ),
    );
  }
  async refreshAccessToken(refreshToken: string) {
    return this.bundleFromTokenResponse(
      await this.tokenRequest(
        new URLSearchParams({
          grant_type: "refresh_token",
          refresh_token: refreshToken,
        }),
      ),
    );
  }
  async exchange(code: string, verifier: string, callback: string) {
    return (await this.exchangeTokens(code, verifier, callback)).accessToken;
  }
  async api(path: string, token: string, params: Record<string, string> = {}) {
    await this.spend();
    const url = new URL(path, this.env.X_API_BASE || "https://api.x.com");
    url.search = new URLSearchParams(params).toString();
    return json(url, { headers: { authorization: `Bearer ${token}` } }, "x");
  }
  async identity(token: string): Promise<Identity> {
    const data = object(
      (
        await this.api("/2/users/me", token, {
          "user.fields":
            "verified,verified_type,public_metrics,profile_image_url",
        })
      ).data,
    );
    const metrics = object(data.public_metrics ?? {});
    return {
      userId: text(data.id),
      handle: text(data.username),
      name: text(data.name),
      avatar: safeAvatar(data.profile_image_url),
      profile: `https://x.com/${encodeURIComponent(text(data.username))}`,
      verified: data.verified === true,
      verifiedType:
        typeof data.verified_type === "string" ? data.verified_type : "",
      followers:
        typeof metrics.followers_count === "number"
          ? Math.max(0, metrics.followers_count)
          : 0,
    };
  }
  async follows(
    userId: string,
    token: string,
    store: Store,
    ownerToken?: string,
  ) {
    if (
      (this.env.FOLLOW_CHECK_MODE || "owner_followers") === "user_following"
    ) {
      let cursor = "";
      for (let page = 0; page < 5; page++) {
        const data = await this.api(
          `/2/users/${encodeURIComponent(userId)}/following`,
          token,
          {
            max_results: "1000",
            ...(cursor ? { pagination_token: cursor } : {}),
          },
        );
        if (
          list(data.data).some((item) => item.id === this.env.OWNER_X_USER_ID)
        )
          return true;
        cursor = String(object(data.meta ?? {}).next_token ?? "");
        if (!cursor) break;
      }
      return false;
    }
    if ((await store.list("owner_followers", { x_user_id: userId })).length)
      return true;
    if (!ownerToken) throw new GateError(503, "x_unavailable");
    let cursor = "";
    for (let page = 0; page < 10; page++) {
      const data = await this.api(
        `/2/users/${encodeURIComponent(this.env.OWNER_X_USER_ID || "")}/followers`,
        ownerToken,
        {
          max_results: "1000",
          ...(cursor ? { pagination_token: cursor } : {}),
        },
      );
      let known = false;
      const ids: string[] = [];
      for (const item of list(data.data)) {
        const id = text(item.id);
        if ((await store.list("owner_followers", { x_user_id: id })).length) {
          known = true;
          break;
        }
        ids.push(id);
      }
      await store.atomic(
        ids.map((id) => ({
          kind: "insert",
          table: "owner_followers",
          row: { x_user_id: id },
          ignore: true,
        })),
      );
      if (ids.includes(userId)) return true;
      if (known) break;
      cursor = String(object(data.meta ?? {}).next_token ?? "");
      if (!cursor) break;
    }
    return Boolean(
      (await store.list("owner_followers", { x_user_id: userId })).length,
    );
  }
  async post(userId: string, token: string, match: (s: string) => boolean) {
    const data = await this.api(
      `/2/users/${encodeURIComponent(userId)}/tweets`,
      token,
      {
        max_results: "10",
        start_time: new Date(Date.now() - 7 * 86400000).toISOString(),
        exclude: "replies",
        "tweet.fields": "entities",
      },
    );
    return list(data.data).find(
      (item) =>
        match(String(item.text ?? "")) ||
        list(object(item.entities ?? {}).urls).some((url) =>
          match(String(url.expanded_url ?? "")),
        ),
    )?.id;
  }
}
export type PageFetch = (url: URL, init: RequestInit) => Promise<Response>;
function linkedinHost(url: URL) {
  return (
    url.protocol === "https:" &&
    !url.username &&
    !url.password &&
    !url.port &&
    (url.hostname === "linkedin.com" || url.hostname.endsWith(".linkedin.com"))
  );
}
/** postKey provides its exported service behavior. */
export function postKey(raw: string) {
  try {
    const url = new URL(raw);
    if (
      !linkedinHost(url) ||
      url.hostname !== "www.linkedin.com" ||
      !/^\/(posts\/[^/]+|feed\/update\/[^/]+)\/?$/.test(url.pathname)
    )
      throw 0;
    return `https://www.linkedin.com${url.pathname.replace(/\/$/, "")}`;
  } catch {
    throw new GateError(422, "post_url_invalid");
  }
}
const jwksCache = new Map<string, { at: number; keys: Record<string, unknown>[] }>();
async function linkedinJwks(url: string) {
  const cached = jwksCache.get(url);
  if (cached && Date.now() - cached.at < 3600000) return cached.keys;
  const response = await fetch(url, { signal: AbortSignal.timeout(8000) });
  if (!response.ok) throw new Error("jwks_unavailable");
  const data = object(JSON.parse(await limitedBody(response)));
  const keys = list(data.keys);
  jwksCache.set(url, { at: Date.now(), keys });
  return keys;
}
function decodeJwtPart(part: string) {
  return JSON.parse(
    Buffer.from(part.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString(
      "utf8",
    ),
  );
}
/** verifyLinkedInIdToken validates OIDC id_token (RS256, iss, aud, exp, nonce). */
export async function verifyLinkedInIdToken(
  token: string,
  env: NodeJS.ProcessEnv,
  nonce: string,
) {
  const parts = token.split(".");
  if (parts.length !== 3) throw new GateError(400, "invalid_token");
  const header = object(decodeJwtPart(parts[0]));
  const payload = object(decodeJwtPart(parts[1]));
  const keys = await linkedinJwks(
    env.LINKEDIN_JWKS_URL || "https://www.linkedin.com/oauth/openid/jwks",
  );
  const jwk = keys.find((key) => key.kid === header.kid);
  if (!jwk) throw new GateError(400, "invalid_token");
  const key = createPublicKey({ key: jwk, format: "jwk" });
  if (
    !verify(
      "sha256",
      Buffer.from(`${parts[0]}.${parts[1]}`),
      key,
      Buffer.from(parts[2].replace(/-/g, "+").replace(/_/g, "/"), "base64"),
    )
  )
    throw new GateError(400, "invalid_token");
  const now = Math.floor(Date.now() / 1000);
  if (payload.iss !== "https://www.linkedin.com")
    throw new GateError(400, "invalid_token");
  if (payload.aud !== env.LINKEDIN_CLIENT_ID)
    throw new GateError(400, "invalid_token");
  if (typeof payload.exp !== "number" || payload.exp < now)
    throw new GateError(400, "invalid_token");
  if (payload.nonce !== nonce) throw new GateError(400, "invalid_token");
  return text(payload.sub);
}
const normalize = (s: string) =>
  s.normalize("NFKC").replace(/\s+/g, " ").trim().toLocaleLowerCase("en");
function decode(s: string) {
  return s
    .replaceAll("&amp;", "&")
    .replaceAll("&quot;", '"')
    .replaceAll("&#39;", "'");
}
export class LinkedInProvider implements Provider {
  env: NodeJS.ProcessEnv;
  pageFetch: PageFetch;
  constructor(
    env: NodeJS.ProcessEnv,
    pageFetch: PageFetch = (url, init) => fetch(url, init),
  ) {
    this.env = env;
    this.pageFetch = pageFetch;
  }
  authorize(
    state: string,
    _verifier: string,
    callback: string,
    nonce: string,
    _owner?: boolean,
  ) {
    const url = new URL(
      "/oauth/v2/authorization",
      this.env.LINKEDIN_AUTH_BASE || "https://www.linkedin.com",
    );
    url.search = new URLSearchParams({
      response_type: "code",
      client_id: this.env.LINKEDIN_CLIENT_ID || "",
      redirect_uri: callback,
      state,
      scope: "openid profile email",
      nonce,
    }).toString();
    return url;
  }
  async exchange(code: string, _verifier: string, callback: string, nonce?: string) {
    const data = await json(
      new URL(
        "/oauth/v2/accessToken",
        this.env.LINKEDIN_API_BASE || "https://api.linkedin.com",
      ),
      {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          grant_type: "authorization_code",
          code,
          redirect_uri: callback,
          client_id: this.env.LINKEDIN_CLIENT_ID || "",
          client_secret: this.env.LINKEDIN_CLIENT_SECRET || "",
        }),
      },
      "linkedin",
    );
    const access = text(data.access_token);
    if (nonce) {
      const idToken =
        typeof data.id_token === "string" ? data.id_token : undefined;
      if (!idToken) throw new GateError(400, "invalid_token");
      await verifyLinkedInIdToken(idToken, this.env, nonce);
    }
    return access;
  }
  async identity(token: string): Promise<Identity> {
    const data = await json(
      new URL(
        "/v2/userinfo",
        this.env.LINKEDIN_API_BASE || "https://api.linkedin.com",
      ),
      { headers: { authorization: `Bearer ${token}` } },
      "linkedin",
    );
    const sub = text(data.sub);
    return {
      userId: sub,
      handle: text(data.name),
      name: text(data.name),
      avatar: safeAvatar(data.picture),
      profile: null,
      ...(typeof data.email === "string" && data.email_verified === true
        ? { email: data.email }
        : {}),
    };
  }
  async verifyPage(raw: string, name: string, match: (s: string) => boolean) {
    let url = new URL(postKey(raw));
    const signal = AbortSignal.timeout(8000);
    try {
      for (let redirects = 0; redirects <= 3; redirects++) {
        const response = await this.pageFetch(url, {
          redirect: "manual",
          credentials: "omit",
          signal,
          headers: { accept: "text/html" },
        });
        if ([301, 302, 303, 307, 308].includes(response.status)) {
          const next = new URL(response.headers.get("location") || "", url);
          await response.body?.cancel();
          if (!linkedinHost(next) || redirects === 3) return false;
          url = next;
          continue;
        }
        if (response.status !== 200) {
          await response.body?.cancel();
          return false;
        }
        const html = await limitedBody(response);
        let author = "";
        for (const meta of html.matchAll(/<meta\b[^>]*>/gi)) {
          const attrs = Object.fromEntries(
            [...meta[0].matchAll(/([\w:-]+)\s*=\s*["']([^"']*)["']/g)].map(
              (m) => [m[1].toLowerCase(), decode(m[2])],
            ),
          );
          if (attrs.name === "author") author = attrs.content || "";
        }
        for (const script of html.matchAll(
          /<script\b[^>]*type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi,
        )) {
          try {
            const parsed = JSON.parse(script[1]);
            for (const item of Array.isArray(parsed) ? parsed : [parsed]) {
              const value = object(item);
              if (value.author && typeof value.author === "object")
                author = String(object(value.author).name ?? author);
            }
          } catch {}
        }
        return (
          Boolean(author) &&
          normalize(author) === normalize(name) &&
          match(decode(html))
        );
      }
    } catch {
      return false;
    }
    return false;
  }
}
