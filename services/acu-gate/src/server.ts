import { createServer } from "node:http";
import type { IncomingMessage } from "node:http";
import { createPublicKey, randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";
import { MemoryStore, NeonStore, StoreConflict, databaseUrl } from "./store.ts";
import type { Store, Row, Op } from "./store.ts";
import { GateError, object, text } from "./errors.ts";
import {
  XProvider,
  LinkedInProvider,
  opaque,
  pkce,
  postKey,
} from "./providers.ts";
import type { ProviderName, PageFetch, Identity } from "./providers.ts";
import { sha256, issueEntitlement, verifyDeviceSignature } from "./crypto.ts";
import {
  settings,
  config,
  preset,
  encrypt,
  decrypt,
  tokenBlob,
  openBlob,
  emailHash,
  constantEqual,
} from "./settings.ts";
import { Downloads } from "./downloads.ts";
const now = () => Math.floor(Date.now() / 1000);
const iso = () => new Date().toISOString();
const expires = (seconds: number) =>
  new Date(Date.now() + seconds * 1000).toISOString();
const first = async (
  store: Store,
  table: Parameters<Store["list"]>[0],
  where: Row,
) => (await store.list(table, where))[0];
const insert = (
  table: Parameters<Store["list"]>[0],
  row: Row,
  ignore = false,
): Op => ({ kind: "insert", table, row, ignore });
const update = (
  table: Parameters<Store["list"]>[0],
  where: Row,
  row: Row,
  required = false,
): Op => ({ kind: "update", table, where, row, required });
function deviceId(value: unknown) {
  const id = text(value, 64);
  if (!/^[a-f0-9]{64}$/.test(id)) throw new GateError(400, "invalid_device");
  return id;
}
function deviceKey(value: unknown) {
  const encoded = text(value, 200);
  const raw = Buffer.from(encoded, "base64");
  if (raw.length !== 65 || raw[0] !== 4)
    throw new GateError(400, "invalid_device_key");
  try {
    createPublicKey({
      format: "jwk",
      key: {
        kty: "EC",
        crv: "P-256",
        x: raw.subarray(1, 33).toString("base64url"),
        y: raw.subarray(33).toString("base64url"),
      },
    });
  } catch {
    throw new GateError(400, "invalid_device_key");
  }
  return encoded;
}
async function body(req: IncomingMessage) {
  const chunks: Buffer[] = [];
  let bytes = 0;
  for await (const chunk of req) {
    bytes += chunk.length;
    if (bytes > 65536) throw new GateError(413, "request_too_large");
    chunks.push(chunk);
  }
  try {
    return object(JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"));
  } catch (error) {
    if (error instanceof GateError) throw error;
    throw new GateError(400, "invalid_request");
  }
}
function consent(value: unknown, defaultValue = false) {
  if (value === undefined) return defaultValue;
  if (typeof value !== "boolean") throw new GateError(400, "invalid_request");
  return value;
}
/** createGate provides its exported service behavior. */
export function createGate(
  input: NodeJS.ProcessEnv,
  store: Store,
  options: { pageFetch?: PageFetch; downloads?: Downloads } = {},
) {
  const env = settings(input);
  const x = new XProvider(env),
    linkedin = new LinkedInProvider(env, options.pageFetch);
  const providers = { x, linkedin };
  const downloads = options.downloads ?? new Downloads(env);
  let lastCleanup = 0;
  async function cleanup() {
    if (Date.now() - lastCleanup > 60000) {
      await store.purgeExpired(iso());
      lastCleanup = Date.now();
    }
  }
  const buckets = new Map<string, { tokens: number; at: number }>();
  function rate(req: IncomingMessage) {
    const time = Date.now(),
      ip = req.socket.remoteAddress || "unknown";
    for (const [key, b] of buckets)
      if (time - b.at > 120000) buckets.delete(key);
    const bucket = buckets.get(ip) ?? { tokens: 60, at: time };
    bucket.tokens = Math.min(60, bucket.tokens + (time - bucket.at) / 1000);
    bucket.at = time;
    if (bucket.tokens < 1) throw new GateError(429, "rate_limited");
    bucket.tokens--;
    buckets.set(ip, bucket);
  }
  async function session(token: string | undefined) {
    if (!token) throw new GateError(401, "not_signed_in");
    const row = await first(store, "sessions", { token_hash: sha256(token) });
    if (!row) throw new GateError(401, "not_signed_in");
    if (Date.parse(String(row.expires_at)) <= Date.now())
      throw new GateError(401, "session_expired");
    return row;
  }
  function bearer(req: IncomingMessage) {
    return req.headers.authorization?.match(/^Bearer (\S+)$/)?.[1];
  }
  async function auth(req: IncomingMessage) {
    return session(bearer(req));
  }
  async function checkRevoked(account: string) {
    if (await first(store, "revocations", { account_id: account }))
      throw new GateError(403, "revoked");
  }
  function token(binding: Row, unlock: Row) {
    return issueEntitlement(env.ENTITLEMENT_PRIVATE_KEY!, {
      acct: String(binding.account_id),
      prov: unlock.provider === "x" ? "x" : "linkedin",
      handle: String(unlock.handle),
      device_id: String(binding.device_id),
    });
  }
  function bindingError(binding: Row) {
    return new GateError(409, "device_conflict", {
      device_name: binding.device_name,
      bound_at: binding.bound_at,
      rebind_available_at: binding.last_rebind_at
        ? new Date(
            Date.parse(String(binding.last_rebind_at)) + 7 * 86400000,
          ).toISOString()
        : iso(),
    });
  }
  async function ensureDevice(s: Row) {
    const taken = await first(store, "bindings", { device_id: s.device_id });
    if (taken && taken.account_id !== s.account_id)
      throw new GateError(409, "device_taken");
    const bound = await first(store, "bindings", { account_id: s.account_id });
    if (bound && bound.device_id !== s.device_id) throw bindingError(bound);
    return bound;
  }
  async function accountStatus(s: Row, enforce = true) {
    await checkRevoked(String(s.account_id));
    const unlock = await first(store, "unlocks", { account_id: s.account_id });
    if (unlock) {
      let binding = enforce
        ? await ensureDevice(s)
        : await first(store, "bindings", { account_id: s.account_id });
      if (enforce && !binding) {
        const row = {
          account_id: s.account_id,
          device_id: s.device_id,
          device_pubkey: s.device_pubkey,
          device_name: s.device_name,
          bound_at: iso(),
          last_rebind_at: null,
        };
        try {
          await store.atomic([insert("bindings", row)]);
        } catch (error) {
          if (!(error instanceof StoreConflict)) throw error;
        }
        binding = await ensureDevice(s);
      }
      return {
        status: "unlocked",
        entitlement:
          binding?.device_id === s.device_id ? token(binding, unlock) : null,
      };
    }
    const claims = await store.list("pending_claims", {
      account_id: s.account_id,
    });
    if (
      enforce &&
      !claims.some((c) => c.status === "pending") &&
      claims.some((c) => c.status === "rejected")
    )
      throw new GateError(422, "rejected");
    return {
      status: claims.some((c) => c.status === "pending")
        ? "pending_review"
        : "needs_verify",
      entitlement: null,
    };
  }
  async function finish(
    s: Row,
    identity: Row,
    post: string,
    show: boolean,
    extra: Op[] = [],
  ) {
    await checkRevoked(String(s.account_id));
    const bound = await ensureDevice(s);
    const existing = await first(store, "unlocks", {
      account_id: s.account_id,
    });
    const binding = bound ?? {
      account_id: s.account_id,
      device_id: s.device_id,
      device_pubkey: s.device_pubkey,
      device_name: s.device_name,
      bound_at: iso(),
      last_rebind_at: null,
    };
    const unlock = existing ?? {
      account_id: s.account_id,
      provider: identity.provider,
      post_ref: post,
      created_at: iso(),
      handle: identity.handle,
      name: identity.name,
      avatar_url: identity.avatar_url ?? null,
      profile_url: identity.profile_url ?? null,
    };
    const ops = [...extra];
    if (!bound) ops.push(insert("bindings", binding));
    if (!existing) ops.push(insert("unlocks", unlock));
    ops.push(
      update(
        "accounts",
        { id: s.account_id },
        { wall_opt_in: show, wall_opt_in_at: show ? iso() : null },
      ),
    );
    try {
      await store.atomic(ops);
    } catch (error) {
      if (error instanceof StoreConflict) {
        await ensureDevice(s);
        const used = await first(store, "used_posts", { post_key: post });
        if (used && used.account_id !== s.account_id)
          throw new GateError(422, "post_url_used");
        if (await first(store, "unlocks", { account_id: s.account_id }))
          return accountStatus(s);
        throw new GateError(409, "concurrent_change");
      }
      throw error;
    }
    return { status: "unlocked", entitlement: token(binding, unlock) };
  }
  async function profileOps(id: string, info: Identity): Promise<Op[]> {
    const row = {
      identity_id: id,
      verified: info.verified ?? false,
      verified_type: info.verifiedType ?? "",
      followers_count: info.followers ?? 0,
      username: info.handle,
    };
    return [
      (await first(store, "x_profiles", { identity_id: id }))
        ? update("x_profiles", { identity_id: id }, row)
        : insert("x_profiles", row),
    ];
  }
  const match = (provider: ProviderName) => (s: string) =>
    s.toLowerCase().includes(preset.match.hashtag.toLowerCase()) ||
    s.includes(preset.match.ref_links[provider === "x" ? 0 : 1]);
  async function signed(
    data: Record<string, unknown>,
    action: "refresh" | "rebind" | "wall",
    key: string,
    id: string,
    show?: boolean,
  ) {
    await cleanup();
    const ts = data.ts;
    if (
      typeof ts !== "number" ||
      !Number.isSafeInteger(ts) ||
      Math.abs(now() - ts) > 300
    )
      throw new GateError(401, "stale_timestamp");
    const signature = text(data.sig, 256);
    if (!verifyDeviceSignature(key, action, id, ts, signature, show))
      throw new GateError(401, "bad_signature");
    const old = await first(store, "device_replays", { device_id: id, ts });
    if (old && Date.parse(String(old.expires_at)) > Date.now())
      throw new GateError(401, "replay");
    const ops: Op[] = [];
    if (old)
      ops.push({
        kind: "delete",
        table: "device_replays",
        where: { device_id: id, ts },
        required: true,
      });
    ops.push(
      insert("device_replays", { device_id: id, ts, expires_at: expires(600) }),
    );
    try {
      await store.atomic(ops);
    } catch (error) {
      if (error instanceof StoreConflict) throw new GateError(401, "replay");
      throw error;
    }
  }
  async function siteStats() {
    const [unlocks, accounts, hits, github] = await Promise.all([
      store.list("unlocks"),
      store.list("accounts"),
      store.list("download_hits"),
      downloads.get(),
    ]);
    const opted = new Set(
      accounts.filter((a) => a.wall_opt_in === true).map((a) => a.id),
    );
    const redirects = hits.reduce((sum, h) => sum + Number(h.count), 0);
    return {
      downloads: {
        github_dmg: github.count,
        redirect_hits: redirects,
        total: github.count + redirects,
      },
      unlocks: unlocks.length,
      wall: unlocks
        .filter((u) => opted.has(u.account_id))
        .sort((a, b) =>
          String(b.created_at).localeCompare(String(a.created_at)),
        )
        .slice(0, 200)
        .map((u) => ({
          handle: u.handle,
          name: u.name,
          provider: u.provider,
          avatar_url: u.avatar_url,
          profile_url: u.profile_url,
          unlocked_at: u.created_at,
        })),
      updated_at: iso(),
    };
  }
  const server = createServer(async (req, res) => {
    const reply = (status: number, value: unknown) => {
      res.writeHead(status, {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        ...responseHeaders,
      });
      res.end(value === null ? undefined : JSON.stringify(value));
    };
    const responseHeaders: Record<string, string> = {};
    const redirect = (location: string) => {
      res.writeHead(302, {
        location,
        "cache-control": "no-store",
        "referrer-policy": "no-referrer",
      });
      res.end();
    };
    const url = new URL(req.url || "/", env.PUBLIC_BASE_URL);
    const path = url.pathname;
    const method = req.method || "GET";
    async function handlePublic() {
      if (path.startsWith("/v1/public/")) {
        responseHeaders["access-control-allow-origin"] = "*";
        rate(req);
        if (method === "OPTIONS") {
          res.writeHead(204, {
            "access-control-allow-origin": "*",
            "access-control-allow-methods": "GET, POST, OPTIONS",
            "access-control-allow-headers": "Content-Type",
          });
          res.end();
          return;
        }
      }
      if (path === "/healthz" && method === "GET")
        return reply(200, { ok: true });
      if (path === "/v1/config" && method === "GET")
        return reply(200, config());
      if (path === "/v1/public/counter" && method === "GET") {
        responseHeaders["cache-control"] = "public, max-age=60";
        return reply(200, {
          unlocks: (await store.list("unlocks")).length,
          updated_at: iso(),
        });
      }
      if (path === "/v1/public/site-stats" && method === "GET") {
        responseHeaders["cache-control"] = "public, max-age=60";
        return reply(200, await siteStats());
      }
      if (path === "/v1/public/ref-click" && ["GET", "POST"].includes(method)) {
        const ref = url.searchParams.get("ref");
        if (ref === "acu-x" || ref === "acu-li")
          await store.atomic([
            {
              kind: "increment",
              table: "ref_clicks",
              where: { ref, day: iso().slice(0, 10) },
            },
          ]);
        return reply(204, null);
      }
      if (path === "/v1/public/download" && method === "GET") {
        const data = await downloads.get();
        if (!data.url) throw new GateError(503, "download_unavailable");
        await store.atomic([
          {
            kind: "increment",
            table: "download_hits",
            where: { day: iso().slice(0, 10) },
          },
        ]);
        res.setHeader("access-control-allow-origin", "*");
        return redirect(data.url);
      }
    }
    async function handleAuth() {
      const admin = path.startsWith("/v1/admin/");
      if (admin && !constantEqual(bearer(req) || "", env.ADMIN_TOKEN!))
        throw new GateError(401, "not_signed_in");
      const start = path.match(/^\/v1\/auth\/(x|linkedin)\/start$/);
      const owner = path === "/v1/admin/x-owner/start";
      if ((start || owner) && method === "GET") {
        await cleanup();
        const provider: ProviderName =
          start?.[1] === "linkedin" ? "linkedin" : "x";
        let linked: Row | undefined;
        if (url.searchParams.get("link") === "1") {
          linked = await session(url.searchParams.get("session") || undefined);
          if (
            Date.now() - Date.parse(String(linked.expires_at)) + 30 * 86400000 >
            10 * 60000
          )
            throw new GateError(401, "session_expired");
        }
        const state = opaque(),
          verifier = opaque(),
          nonce = opaque();
        const challenge = owner
          ? "owner"
          : text(url.searchParams.get("app_challenge"), 43);
        if (!owner && !/^[\w-]{43}$/.test(challenge))
          throw new GateError(400, "invalid_request");
        const id = owner
          ? "0".repeat(64)
          : deviceId(url.searchParams.get("device_id"));
        if (linked && linked.device_id !== id)
          throw new GateError(409, "device_conflict");
        await store.atomic([
          insert("login_states", {
            state_hash: sha256(state),
            provider,
            verifier,
            nonce,
            app_challenge: challenge,
            device_id: id,
            device_name: owner
              ? "Owner"
              : text(url.searchParams.get("device_name"), 200),
            link_account_id: linked?.account_id ?? null,
            owner_authorization: owner,
            expires_at: expires(600),
          }),
        ]);
        const target = providers[provider].authorize(
          state,
          verifier,
          `${env.PUBLIC_BASE_URL}/v1/auth/${provider}/callback`,
          nonce,
        );
        if (env.DEV === "1" && url.searchParams.has("mock_user"))
          target.searchParams.set(
            "mock_user",
            url.searchParams.get("mock_user")!,
          );
        return redirect(target.href);
      }
      const callback = path.match(/^\/v1\/auth\/(x|linkedin)\/callback$/);
      if (callback && method === "GET") {
        try {
          const provider: ProviderName = callback[1] === "x" ? "x" : "linkedin";
          const state = await first(store, "login_states", {
            state_hash: sha256(text(url.searchParams.get("state"))),
          });
          if (
            !state ||
            state.provider !== provider ||
            Date.parse(String(state.expires_at)) <= Date.now()
          )
            throw new GateError(400, "invalid_state");
          await store.atomic([
            {
              kind: "delete",
              table: "login_states",
              where: { state_hash: state.state_hash },
              required: true,
            },
          ]);
          if (url.searchParams.has("error"))
            throw new GateError(400, "authorization_denied");
          const access = await providers[provider].exchange(
            text(url.searchParams.get("code")),
            String(state.verifier),
            `${env.PUBLIC_BASE_URL}/v1/auth/${provider}/callback`,
          );
          const info = await providers[provider].identity(access);
          if (state.owner_authorization === true) {
            if (provider !== "x" || info.userId !== env.OWNER_X_USER_ID)
              throw new GateError(403, "owner_mismatch");
            const row = {
              provider: "x",
              ...encrypt(access, env.TOKEN_ENC_KEY!),
            };
            await store.atomic([
              (await first(store, "owner_tokens", { provider: "x" }))
                ? update("owner_tokens", { provider: "x" }, row)
                : insert("owner_tokens", row),
            ]);
            return reply(200, { ok: true });
          }
          let identity = await first(store, "identities", {
            provider,
            provider_user_id: info.userId,
          });
          if (
            identity &&
            state.link_account_id &&
            identity.account_id !== state.link_account_id
          )
            throw new GateError(409, "identity_taken");
          const account =
            identity?.account_id ?? state.link_account_id ?? randomUUID();
          const id = identity?.id ?? randomUUID();
          const fields = {
            handle: info.handle,
            name: info.name,
            avatar_url: info.avatar,
            profile_url: info.profile,
            email_hash: info.email
              ? emailHash(info.email, env.EMAIL_HASH_KEY!)
              : null,
            token_blob: tokenBlob(access, env.TOKEN_ENC_KEY!),
          };
          const ops: Op[] = [];
          if (!identity && !state.link_account_id)
            ops.push(
              insert("accounts", {
                id: account,
                created_at: iso(),
                wall_opt_in: false,
                wall_opt_in_at: null,
              }),
            );
          if (identity) ops.push(update("identities", { id }, fields));
          else
            ops.push(
              insert("identities", {
                id,
                account_id: account,
                provider,
                provider_user_id: info.userId,
                ...fields,
              }),
            );
          if (provider === "x")
            ops.push(...(await profileOps(String(id), info)));
          const code = opaque();
          ops.push(
            insert("login_codes", {
              code_hash: sha256(code),
              account_id: account,
              identity_id: id,
              app_challenge: state.app_challenge,
              device_id: state.device_id,
              device_name: state.device_name,
              expires_at: expires(60),
            }),
          );
          await store.atomic(ops);
          return redirect(`${env.APP_CALLBACK_SCHEME}://auth?code=${code}`);
        } catch (error) {
          return redirect(
            `${env.APP_CALLBACK_SCHEME}://auth?error=${
              error instanceof GateError ? error.code : "server_error"
            }`,
          );
        }
      }
    }
    async function handleSessionVerify() {
      if (path === "/v1/session/redeem" && method === "POST") {
        const data = await body(req),
          code = await first(store, "login_codes", {
            code_hash: sha256(text(data.code)),
          });
        if (!code || Date.parse(String(code.expires_at)) <= Date.now())
          throw new GateError(401, "invalid_code");
        if (
          !constantEqual(
            pkce(text(data.app_verifier, 128)),
            String(code.app_challenge),
          ) ||
          deviceId(data.device_id) !== code.device_id
        )
          throw new GateError(401, "invalid_code");
        const rawKey = deviceKey(data.device_pubkey),
          plain = opaque();
        const s = {
          token_hash: sha256(plain),
          account_id: code.account_id,
          identity_id: code.identity_id,
          device_id: code.device_id,
          device_name: code.device_name,
          device_pubkey: rawKey,
          expires_at: expires(30 * 86400),
        };
        const existing = await first(store, "bindings", {
          device_id: s.device_id,
        });
        if (existing && existing.account_id !== s.account_id)
          throw new GateError(409, "device_taken");
        if (existing && existing.device_pubkey !== rawKey)
          throw new GateError(409, "device_key_conflict");
        try {
          await store.atomic([
            {
              kind: "delete",
              table: "login_codes",
              where: { code_hash: code.code_hash },
              required: true,
            },
            insert("sessions", s),
          ]);
        } catch (error) {
          if (error instanceof StoreConflict)
            throw new GateError(401, "invalid_code");
          throw error;
        }
        const identity = await first(store, "identities", {
          id: s.identity_id,
        });
        return reply(200, {
          session: plain,
          account: {
            id: s.account_id,
            provider: identity.provider,
            handle: identity.handle,
            name: identity.name,
            linked: (
              await store.list("identities", { account_id: s.account_id })
            ).map((i) => i.provider),
          },
          ...(await accountStatus(s, false)),
          share_links: config(),
        });
      }
      if (path === "/v1/status" && method === "GET")
        return reply(200, await accountStatus(await auth(req)));
      if (path === "/v1/verify/x" && method === "POST") {
        const s = await auth(req),
          data = await body(req);
        const show = consent(data.show_on_wall);
        await checkRevoked(String(s.account_id));
        const identity = await first(store, "identities", {
          account_id: s.account_id,
          provider: "x",
        });
        if (!identity) throw new GateError(401, "not_signed_in");
        const access = openBlob(
            String(identity.token_blob),
            env.TOKEN_ENC_KEY!,
          ),
          info = await x.identity(access);
        const fields = {
          handle: info.handle,
          name: info.name,
          avatar_url: info.avatar,
          profile_url: info.profile,
        };
        await store.atomic([
          update("identities", { id: identity.id }, fields),
          ...(await profileOps(String(identity.id), info)),
        ]);
        Object.assign(identity, fields);
        const owner = await first(store, "owner_tokens", { provider: "x" });
        const ownerAccess = owner
          ? decrypt(
              {
                ciphertext: String(owner.ciphertext),
                nonce: String(owner.nonce),
                tag: String(owner.tag),
              },
              env.TOKEN_ENC_KEY!,
            )
          : undefined;
        if (!(await x.follows(info.userId, access, store, ownerAccess)))
          throw new GateError(422, "follow_missing");
        const post = await x.post(info.userId, access, match("x"));
        if (!post) throw new GateError(422, "post_missing");
        const key = `x:${post}`;
        const used = await first(store, "used_posts", { post_key: key });
        if (used && used.account_id !== s.account_id)
          throw new GateError(422, "post_url_used");
        return reply(
          200,
          await finish(
            s,
            identity,
            key,
            show,
            used
              ? []
              : [
                  insert("used_posts", {
                    post_key: key,
                    account_id: s.account_id,
                    created_at: iso(),
                  }),
                ],
          ),
        );
      }
      if (path === "/v1/verify/linkedin" && method === "POST") {
        const s = await auth(req),
          data = await body(req);
        const show = consent(data.show_on_wall);
        await checkRevoked(String(s.account_id));
        await ensureDevice(s);
        const identity = await first(store, "identities", {
          account_id: s.account_id,
          provider: "linkedin",
        });
        if (!identity) throw new GateError(401, "not_signed_in");
        const key = postKey(text(data.post_url));
        const used = await first(store, "used_posts", { post_key: key });
        if (used) {
          if (used.account_id !== s.account_id)
            throw new GateError(422, "post_url_used");
          const claim = await first(store, "pending_claims", { post_key: key });
          if (claim?.status === "rejected")
            throw new GateError(422, "rejected");
          if (claim?.status === "pending")
            return reply(202, { status: "pending_review" });
          return reply(200, await accountStatus(s));
        }
        const reserve = insert("used_posts", {
          post_key: key,
          account_id: s.account_id,
          created_at: iso(),
        });
        if (
          await linkedin.verifyPage(
            key,
            String(identity.name),
            match("linkedin"),
          )
        )
          return reply(200, await finish(s, identity, key, show, [reserve]));
        try {
          await store.atomic([
            reserve,
            insert("pending_claims", {
              id: randomUUID(),
              account_id: s.account_id,
              identity_id: identity.id,
              post_key: key,
              status: "pending",
              created_at: iso(),
            }),
            update(
              "accounts",
              { id: s.account_id },
              { wall_opt_in: show, wall_opt_in_at: show ? iso() : null },
            ),
          ]);
        } catch (error) {
          if (error instanceof StoreConflict)
            throw new GateError(422, "post_url_used");
          throw error;
        }
        return reply(202, { status: "pending_review" });
      }
    }
    async function handleDevice() {
      if (path === "/v1/device/rebind" && method === "POST") {
        const s = await auth(req),
          data = await body(req),
          id = deviceId(data.device_id);
        await checkRevoked(String(s.account_id));
        if (
          id !== s.device_id ||
          Date.now() - Date.parse(String(s.expires_at)) + 30 * 86400000 >
            10 * 60000
        )
          throw new GateError(401, "fresh_sign_in_required");
        await signed(data, "rebind", String(s.device_pubkey), id);
        const bound = await first(store, "bindings", {
          account_id: s.account_id,
        });
        const unlock = await first(store, "unlocks", {
          account_id: s.account_id,
        });
        if (!bound || !unlock) throw new GateError(404, "not_found");
        if (
          bound.last_rebind_at &&
          Date.parse(String(bound.last_rebind_at)) + 7 * 86400000 > Date.now()
        )
          throw new GateError(429, "rebind_cooldown", {
            rebind_available_at: new Date(
              Date.parse(String(bound.last_rebind_at)) + 7 * 86400000,
            ).toISOString(),
          });
        const taken = await first(store, "bindings", { device_id: id });
        if (taken && taken.account_id !== s.account_id)
          throw new GateError(409, "device_taken");
        const row = {
          device_id: id,
          device_pubkey: s.device_pubkey,
          device_name: s.device_name,
          bound_at: iso(),
          last_rebind_at: iso(),
        };
        try {
          await store.atomic([
            insert(
              "moved_devices",
              { device_id: bound.device_id, account_id: s.account_id },
              true,
            ),
            {
              kind: "delete",
              table: "moved_devices",
              where: { device_id: id },
            },
            update(
              "bindings",
              {
                account_id: s.account_id,
                device_id: bound.device_id,
                last_rebind_at: bound.last_rebind_at,
              },
              row,
              true,
            ),
          ]);
        } catch (error) {
          if (error instanceof StoreConflict)
            throw new GateError(409, "concurrent_change");
          throw error;
        }
        return reply(200, {
          status: "unlocked",
          entitlement: token({ ...bound, ...row }, unlock),
        });
      }
      if (path === "/v1/entitlement/refresh" && method === "POST") {
        const data = await body(req),
          id = deviceId(data.device_id);
        const binding = await first(store, "bindings", { device_id: id });
        if (!binding) {
          if (await first(store, "moved_devices", { device_id: id }))
            throw new GateError(409, "device_moved");
          throw new GateError(404, "not_found");
        }
        await signed(data, "refresh", String(binding.device_pubkey), id);
        await checkRevoked(String(binding.account_id));
        const unlock = await first(store, "unlocks", {
          account_id: binding.account_id,
        });
        if (!unlock) throw new GateError(404, "not_found");
        return reply(200, { entitlement: token(binding, unlock) });
      }
      if (path === "/v1/wall" && method === "POST") {
        const data = await body(req),
          show = consent(data.show);
        let account: string;
        if (typeof data.show !== "boolean")
          throw new GateError(400, "invalid_request");
        if (bearer(req)) account = String((await auth(req)).account_id);
        else {
          const id = deviceId(data.device_id);
          const binding = await first(store, "bindings", { device_id: id });
          if (!binding) throw new GateError(404, "not_found");
          await signed(data, "wall", String(binding.device_pubkey), id, show);
          account = String(binding.account_id);
        }
        await checkRevoked(account);
        await store.atomic([
          update(
            "accounts",
            { id: account },
            { wall_opt_in: show, wall_opt_in_at: show ? iso() : null },
            true,
          ),
        ]);
        return reply(200, { show });
      }
    }
    async function handleAdmin() {
      const admin = path.startsWith("/v1/admin/");
      if (admin) {
        if (path === "/v1/admin/pending" && method === "GET")
          return reply(200, {
            pending: await store.list("pending_claims", { status: "pending" }),
          });
        const review = path.match(
          /^\/v1\/admin\/pending\/([\w-]+)\/(approve|reject)$/,
        );
        if (review && method === "POST") {
          const claim = await first(store, "pending_claims", { id: review[1] });
          if (!claim) throw new GateError(404, "not_found");
          if (claim.status !== "pending")
            throw new GateError(409, "already_reviewed");
          if (review[2] === "reject") {
            await store.atomic([
              update(
                "pending_claims",
                { id: claim.id, status: "pending" },
                { status: "rejected" },
                true,
              ),
            ]);
            return reply(200, { status: "rejected" });
          }
          const identity = await first(store, "identities", {
            id: claim.identity_id,
          });
          const account = await first(store, "accounts", {
            id: claim.account_id,
          });
          // Approval records the unlock; the authenticated app binds its current Mac on status.
          const existing = await first(store, "unlocks", {
            account_id: claim.account_id,
          });
          await checkRevoked(String(claim.account_id));
          const ops: Op[] = [
            update(
              "pending_claims",
              { id: claim.id, status: "pending" },
              { status: "approved" },
              true,
            ),
          ];
          if (!existing)
            ops.push(
              insert("unlocks", {
                account_id: claim.account_id,
                provider: "linkedin",
                post_ref: claim.post_key,
                created_at: iso(),
                handle: identity.handle,
                name: identity.name,
                avatar_url: identity.avatar_url ?? null,
                profile_url: identity.profile_url ?? null,
              }),
            );
          await store.atomic(ops);
          return reply(200, {
            status: "unlocked",
            wall_opt_in: account.wall_opt_in,
          });
        }
        const action = path.match(
          /^\/v1\/admin\/accounts\/([\w-]+)\/(revoke|reset-rebind)$/,
        );
        if (action && method === "POST") {
          if (!(await first(store, "accounts", { id: action[1] })))
            throw new GateError(404, "not_found");
          await store.atomic(
            action[2] === "revoke"
              ? [
                  insert(
                    "revocations",
                    { account_id: action[1], created_at: iso() },
                    true,
                  ),
                  update(
                    "accounts",
                    { id: action[1] },
                    { wall_opt_in: false, wall_opt_in_at: null },
                  ),
                ]
              : [
                  update(
                    "bindings",
                    { account_id: action[1] },
                    { last_rebind_at: null },
                  ),
                ],
          );
          return reply(200, { ok: true });
        }
        if (path === "/v1/admin/stats" && method === "GET") {
          const stats = await siteStats();
          const unlocks = await store.list("unlocks");
          const profiles = await store.list("x_profiles");
          const identities = await store.list("identities");
          const refs = await store.list("ref_clicks");
          const xAccounts = new Set(
            unlocks.filter((u) => u.provider === "x").map((u) => u.account_id),
          );
          const ids = new Set(
            identities
              .filter((i) => i.provider === "x" && xAccounts.has(i.account_id))
              .map((i) => i.id),
          );
          const selected = profiles.filter((p) => ids.has(p.identity_id));
          const byType: Record<string, number> = {};
          for (const profile of selected.filter((p) => p.verified === true)) {
            const type = String(profile.verified_type || "unknown");
            byType[type] = (byType[type] || 0) + 1;
          }
          return reply(200, {
            unlocks: {
              x: unlocks.filter((u) => u.provider === "x").length,
              linkedin: unlocks.filter((u) => u.provider === "linkedin").length,
              total: unlocks.length,
            },
            verified_unlocks: selected.filter((p) => p.verified === true)
              .length,
            verified_by_type: byType,
            reach: selected.reduce(
              (sum, profile) => sum + Number(profile.followers_count),
              0,
            ),
            pending: (await store.list("pending_claims", { status: "pending" }))
              .length,
            ref_clicks: {
              "acu-x": refs
                .filter((ref) => ref.ref === "acu-x")
                .reduce((count, ref) => count + Number(ref.count), 0),
              "acu-li": refs
                .filter((ref) => ref.ref === "acu-li")
                .reduce((count, ref) => count + Number(ref.count), 0),
            },
            ref_clicks_by_day: refs.sort((a, b) =>
              String(a.day).localeCompare(String(b.day)),
            ),
            downloads: stats.downloads,
            wall_opt_in: (await store.list("accounts", { wall_opt_in: true }))
              .length,
          });
        }
      }
      throw new GateError(404, "not_found");
    }
    try {
      await handlePublic();
      if (res.writableEnded) return;
      await handleAuth();
      if (res.writableEnded) return;
      await handleSessionVerify();
      if (res.writableEnded) return;
      await handleDevice();
      if (res.writableEnded) return;
      await handleAdmin();
    } catch (error) {
      if (error instanceof GateError)
        return reply(error.status, {
          error: error.code,
          message: error.message,
          ...error.extra,
        });
      if (error instanceof StoreConflict)
        return reply(409, {
          error: "concurrent_change",
          message: "Retry the request.",
        });
      return reply(500, {
        error: "server_error",
        message: "The request could not be completed.",
      });
    }
  });
  return { server, env, store };
}
/** main provides its exported service behavior. */
export async function main() {
  const env = settings(process.env),
    url = await databaseUrl(env);
  if (!url && env.DEV !== "1") throw new Error("database_url_required");
  const store = url ? new NeonStore(url) : new MemoryStore();
  const { server } = createGate(env, store);
  server.listen(Number(env.PORT || 10000), "0.0.0.0");
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href)
  main().catch((error) => {
    const message =
      error instanceof Error &&
      /^Missing required setting: [A-Z_]+$/.test(error.message)
        ? error.message
        : "required configuration or database unavailable";
    process.stderr.write(`ACU gate startup failed: ${message}\n`);
    process.exitCode = 1;
  });
