import { createServer } from "node:http";
import type { Server } from "node:http";
import { once } from "node:events";
import {
  generateKeyPairSync,
  createSign,
} from "node:crypto";
import { opaque, pkce } from "./providers.ts";
import type { PageFetch } from "./providers.ts";
export type MockUser = {
  name: string;
  follows: boolean;
  post: "hashtag" | "expanded" | "none";
  verified: boolean;
  avatar: string;
  followers: number;
};
/** startMocks provides its exported service behavior. */
export async function startMocks() {
  const users = new Map<string, MockUser>(),
    codes = new Map<
      string,
      {
        user: string;
        challenge: string;
        redirect: string;
        nonce?: string;
      }
    >(),
    tokens = new Map<string, string>();
  const pages = new Map<
    string,
    { status?: number; author?: string; body?: string; redirect?: string }
  >();
  const linkedinKeys = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const linkedinJwk = linkedinKeys.publicKey.export({ format: "jwk" }) as Record<
    string,
    string
  >;
  linkedinJwk.kid = "mock-li";
  linkedinJwk.alg = "RS256";
  linkedinJwk.use = "sig";
  function signIdToken(user: string, nonce: string, clientId: string) {
    const header = Buffer.from(
      JSON.stringify({ alg: "RS256", typ: "JWT", kid: "mock-li" }),
    ).toString("base64url");
    const now = Math.floor(Date.now() / 1000);
    const payload = Buffer.from(
      JSON.stringify({
        iss: "https://www.linkedin.com",
        aud: clientId,
        sub: `li:${user}`,
        exp: now + 3600,
        nonce,
      }),
    ).toString("base64url");
    const data = `${header}.${payload}`;
    const sig = createSign("RSA-SHA256")
      .update(data)
      .sign(linkedinKeys.privateKey)
      .toString("base64url");
    return `${data}.${sig}`;
  }
  const state = {
    xStatus: 200,
    githubStatus: 200,
    githubCalls: 0,
    followersCalls: 0,
    pageCalls: 0,
    followersPages: [["guru_test"]] as string[][],
    githubPages: [] as unknown[][],
  };
  function user(id: string) {
    let value = users.get(id);
    if (!value) {
      value = {
        name: id === "guru_test" ? "Guru Test" : id,
        follows: !id.includes("no_follow"),
        post: id.includes("no_post")
          ? "none"
          : id.includes("expanded")
            ? "expanded"
            : "hashtag",
        verified: true,
        avatar: "https://pbs.twimg.com/profile_images/test.png",
        followers: 123,
      };
      users.set(id, value);
    }
    return value;
  }
  const server = createServer(async (req, res) => {
    const url = new URL(req.url || "/", base);
    const json = (status: number, data: unknown) => {
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify(data));
    };
    if (
      url.pathname === "/i/oauth2/authorize" ||
      url.pathname === "/oauth/v2/authorization"
    ) {
      const id = url.searchParams.get("mock_user") || "guru_test",
        code = opaque();
      user(id);
      codes.set(code, {
        user: id,
        challenge: url.searchParams.get("code_challenge") || "",
        redirect: url.searchParams.get("redirect_uri") || "",
        nonce: url.searchParams.get("nonce") || undefined,
      });
      const redirect = new URL(url.searchParams.get("redirect_uri")!);
      redirect.search = new URLSearchParams({
        code,
        state: url.searchParams.get("state") || "",
      }).toString();
      res.writeHead(302, { location: redirect.href });
      res.end();
      return;
    }
    if (
      url.pathname === "/2/oauth2/token" ||
      url.pathname === "/oauth/v2/accessToken"
    ) {
      let raw = "";
      for await (const chunk of req) raw += chunk;
      const data = new URLSearchParams(raw),
        code = codes.get(data.get("code") || "");
      if (
        !code ||
        code.redirect !== data.get("redirect_uri") ||
        (code.challenge &&
          (pkce(data.get("code_verifier") || "") !== code.challenge ||
            !req.headers.authorization?.startsWith("Basic "))) ||
        (!code.challenge && !data.get("client_secret"))
      )
        return json(400, { error: "invalid_grant" });
      codes.delete(data.get("code")!);
      const token = opaque();
      tokens.set(token, code.user);
      if (url.pathname === "/oauth/v2/accessToken") {
        return json(200, {
          access_token: token,
          token_type: "bearer",
          id_token: signIdToken(
            code.user,
            code.nonce || "",
            data.get("client_id") || "",
          ),
        });
      }
      return json(200, { access_token: token, token_type: "bearer" });
    }
    if (url.pathname === "/oauth/openid/jwks")
      return json(200, { keys: [linkedinJwk] });
    if (url.pathname.startsWith("/2/")) {
      if (state.xStatus !== 200)
        return json(state.xStatus, { error: "mock_failure" });
      const id = tokens.get(
        req.headers.authorization?.replace("Bearer ", "") || "",
      );
      if (!id) return json(401, {});
      const value = user(id);
      if (url.pathname === "/2/users/me")
        return json(200, {
          data: {
            id,
            username: id,
            name: value.name,
            verified: value.verified,
            verified_type: value.verified ? "blue" : "",
            public_metrics: { followers_count: value.followers },
            profile_image_url: value.avatar,
          },
        });
      if (url.pathname.endsWith("/followers")) {
        state.followersCalls++;
        const page = Number(url.searchParams.get("pagination_token") || 0);
        return json(200, {
          data: (state.followersPages[page] || []).map((id) => ({ id })),
          meta:
            page + 1 < state.followersPages.length
              ? { next_token: String(page + 1) }
              : {},
        });
      }
      if (url.pathname.endsWith("/following"))
        return json(200, {
          data: value.follows ? [{ id: "owner" }] : [],
          meta: {},
        });
      if (url.pathname.endsWith("/tweets"))
        return json(200, {
          data:
            value.post === "none"
              ? []
              : [
                  {
                    id: `post-${id}`,
                    text:
                      value.post === "hashtag"
                        ? "Using #aGeNtCoMpUtErUsE"
                        : "https://t.co/abc",
                    entities: {
                      urls:
                        value.post === "expanded"
                          ? [
                              {
                                expanded_url:
                                  "https://ingpoc-agent-plugins.nicewasp128.chatgpt.site/?ref=acu-x",
                              },
                            ]
                          : [],
                    },
                  },
                ],
        });
    }
    if (url.pathname === "/v2/userinfo") {
      const id = tokens.get(
        req.headers.authorization?.replace("Bearer ", "") || "",
      );
      if (!id) return json(401, {});
      const value = user(id);
      return json(200, {
        sub: `li:${id}`,
        name: value.name,
        email: `${id}@example.invalid`,
        email_verified: true,
        picture: value.avatar.replace("pbs.twimg.com", "media.licdn.com"),
      });
    }
    if (url.pathname.startsWith("/public/")) {
      state.pageCalls++;
      const page = pages.get(url.pathname.slice("/public".length)) ?? {};
      if (page.redirect) {
        res.writeHead(302, { location: page.redirect });
        res.end();
        return;
      }
      res.writeHead(page.status || 200, { "content-type": "text/html" });
      res.end(
        page.body ??
          `<html><meta name="author" content="${page.author || "Guru Test"}"><p>#AgentComputerUse</p></html>`,
      );
      return;
    }
    if (url.pathname === "/repos/ingpoc/agent-plugins/releases") {
      state.githubCalls++;
      if (state.githubStatus !== 200) return json(state.githubStatus, {});
      const index = Number(url.searchParams.get("page") || 1) - 1;
      if (index + 1 < state.githubPages.length)
        res.setHeader(
          "link",
          `<${base}/repos/ingpoc/agent-plugins/releases?per_page=100&page=${index + 2}>; rel="next"`,
        );
      return json(200, state.githubPages[index] || []);
    }
    json(404, {});
  });
  let base = "http://127.0.0.1";
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string")
    throw new Error("mock_listen_failed");
  base = `http://127.0.0.1:${address.port}`;
  const pageFetch: PageFetch = (url, init) =>
    fetch(new URL(`/public${url.pathname}${url.search}`, base), init);
  return {
    server,
    base,
    users,
    user,
    pages,
    state,
    pageFetch,
    env: {
      X_AUTH_BASE: base,
      X_API_BASE: base,
      LINKEDIN_AUTH_BASE: base,
      LINKEDIN_API_BASE: base,
      LINKEDIN_JWKS_URL: `${base}/oauth/openid/jwks`,
      GITHUB_API_BASE: base,
    },
    close: () => closeServer(server),
  };
}
/** closeServer provides its exported service behavior. */
export async function closeServer(server: Server) {
  server.closeAllConnections();
  if (server.listening)
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
}
