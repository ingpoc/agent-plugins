import { createServer } from "node:http";
import type { IncomingMessage } from "node:http";
import { createPublicKey, randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";
import { MemoryStore, NeonStore, StoreConflict, databaseUrl } from "./store.ts";
import type { Store, Row, Op } from "./store.ts";
import { GateError, object, text } from "./errors.ts";
import {
  sha256Hex,
  issueEntitlement,
  verifyDeviceSignature,
  safeAvatar,
  type DeviceAction,
} from "./crypto.ts";
import { settings, config, preset, constantEqual } from "./settings.ts";
import { Downloads } from "./downloads.ts";
import { clientIp } from "./client-ip.ts";
import { createOEmbedClient, classifyOEmbedHttpStatus } from "./oembed.ts";
import type { OEmbedClient } from "./oembed.ts";
import {
  defaultPageFetch,
  fetchLinkedInPublicPost,
  type PageFetch,
} from "./linkedin-page.ts";
import {
  normalizeHandle,
  parseXStatusUrl,
  validateLinkedInPostUrl,
  snowflakeMs,
  type Provider,
} from "./handles.ts";

const CLAIM_TTL_MS = 72 * 3600000;
const REBIND_COOLDOWN_MS = 7 * 86400000;
const now = () => Math.floor(Date.now() / 1000);
const iso = () => new Date().toISOString();
const expires = (seconds: number) =>
  new Date(Date.now() + seconds * 1000).toISOString();
const first = async (store: Store, table: Parameters<Store["list"]>[0], where: Row) =>
  (await store.list(table, where))[0];
const insert = (table: Parameters<Store["list"]>[0], row: Row, ignore = false): Op => ({
  kind: "insert",
  table,
  row,
  ignore,
});
const update = (
  table: Parameters<Store["list"]>[0],
  where: Row,
  row: Row,
  required = false,
): Op => ({ kind: "update", table, where, row, required });

function deviceIdHeader(value: string | undefined) {
  const id = text(value ?? "", 64);
  if (!/^[a-f0-9]{64}$/.test(id)) throw new GateError(401, "bad_signature");
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
async function readRawBody(req: IncomingMessage) {
  const chunks: Buffer[] = [];
  let bytes = 0;
  for await (const chunk of req) {
    bytes += chunk.length;
    if (bytes > 65536) throw new GateError(413, "request_too_large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}
async function body(req: IncomingMessage) {
  const raw = await readRawBody(req);
  try {
    return object(JSON.parse(raw.toString("utf8") || "{}"));
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
function checksPayload(claim: Row) {
  return {
    follow: claim.follow_check,
    post: claim.post_check,
  };
}
function matchText(textValue: string, provider: Provider) {
  const ref = preset.match.ref_links[provider === "x" ? 0 : 1];
  return (
    textValue.toLowerCase().includes(preset.match.hashtag.toLowerCase()) ||
    textValue.includes(ref)
  );
}

/** createGate wires HTTP handlers to the gate store and dependencies. */
export function createGate(
  input: NodeJS.ProcessEnv,
  store: Store,
  options: {
    pageFetch?: PageFetch;
    downloads?: Downloads;
    oembed?: OEmbedClient;
  } = {},
) {
  const env = settings(input);
  const trustHops = Number(env.TRUST_PROXY_HOPS || 0);
  const oembed = options.oembed ?? createOEmbedClient(env);
  const pageFetch = options.pageFetch ?? defaultPageFetch;
  const downloads = options.downloads ?? new Downloads(env);
  let lastCleanup = 0;
  async function cleanup() {
    if (Date.now() - lastCleanup > 60000) {
      await store.purgeExpired(iso());
      lastCleanup = Date.now();
    }
  }
  const buckets = new Map<string, { tokens: number; at: number }>();
  const pollBuckets = new Map<string, { tokens: number; at: number }>();
  const adminFails = new Map<string, { count: number; at: number }>();
  function rateLimited(retryAfter: number) {
    throw new GateError(429, "rate_limited", {
      retry_after: Math.max(1, Math.ceil(retryAfter)),
    });
  }
  function rateLimit(
    name: string,
    max: number,
    windowMs: number,
    req: IncomingMessage,
    keySuffix = "",
  ) {
    const time = Date.now(),
      ip = clientIp(req, trustHops),
      key = `${name}:${ip}${keySuffix}`;
    for (const [k, b] of buckets)
      if (time - b.at > windowMs * 2) buckets.delete(k);
    const rate = max / (windowMs / 1000);
    const bucket = buckets.get(key) ?? { tokens: max, at: time };
    bucket.tokens = Math.min(max, bucket.tokens + ((time - bucket.at) / 1000) * rate);
    bucket.at = time;
    if (bucket.tokens < 1) rateLimited((1 - bucket.tokens) / rate);
    bucket.tokens -= 1;
    buckets.set(key, bucket);
  }
  function rateDevice(name: string, max: number, windowMs: number, deviceId: string) {
    const time = Date.now(),
      key = `${name}:${deviceId}`;
    for (const [k, b] of pollBuckets)
      if (time - b.at > windowMs * 2) pollBuckets.delete(k);
    const rate = max / (windowMs / 1000);
    const bucket = pollBuckets.get(key) ?? { tokens: max, at: time };
    bucket.tokens = Math.min(max, bucket.tokens + ((time - bucket.at) / 1000) * rate);
    bucket.at = time;
    if (bucket.tokens < 1) rateLimited((1 - bucket.tokens) / rate);
    bucket.tokens -= 1;
    pollBuckets.set(key, bucket);
  }
  function adminAuthFail(req: IncomingMessage) {
    const time = Date.now(),
      ip = clientIp(req, trustHops);
    const entry = adminFails.get(ip) ?? { count: 0, at: time };
    if (time - entry.at > 60000) {
      entry.count = 0;
      entry.at = time;
    }
    entry.count += 1;
    adminFails.set(ip, entry);
    if (entry.count > 10) rateLimited(60);
    throw new GateError(401, "not_found");
  }
  function bearer(req: IncomingMessage) {
    return req.headers.authorization?.match(/^Bearer (\S+)$/)?.[1];
  }
  async function checkRevoked(account: string) {
    if (await first(store, "revocations", { account_id: account }))
      throw new GateError(403, "revoked");
  }
  function profileUrl(provider: Provider, handle: string) {
    return provider === "x"
      ? `https://x.com/${handle}`
      : `https://www.linkedin.com/in/${handle}`;
  }
  function token(binding: Row, unlock: Row, account: Row) {
    return issueEntitlement(env.ENTITLEMENT_PRIVATE_KEY!, {
      acct: String(binding.account_id),
      prov: unlock.provider === "x" ? "x" : "linkedin",
      handle: String(unlock.handle),
      device_id: String(binding.device_id),
    });
  }
  async function signedRequest(
    req: IncomingMessage,
    action: DeviceAction,
    pubkey: string,
    bodyRaw: Buffer,
  ) {
    await cleanup();
    const id = deviceIdHeader(req.headers["x-acu-device"] as string | undefined);
    const tsRaw = req.headers["x-acu-ts"];
    const ts = Number(tsRaw);
    if (!Number.isSafeInteger(ts) || Math.abs(now() - ts) > 300)
      throw new GateError(401, "bad_signature");
    const sig = text(req.headers["x-acu-sig"] as string | undefined, 256);
    const bodyHash = sha256Hex(bodyRaw.toString("utf8"));
    if (!verifyDeviceSignature(pubkey, action, id, ts, sig, bodyHash))
      throw new GateError(401, "bad_signature");
    const sigHash = sha256Hex(Buffer.from(sig, "base64"));
    const old = await first(store, "device_replays", { device_id: id, sig_hash: sigHash });
    if (old && Date.parse(String(old.expires_at)) > Date.now())
      throw new GateError(401, "bad_signature");
    const ops: Op[] = [];
    if (old)
      ops.push({
        kind: "delete",
        table: "device_replays",
        where: { device_id: id, sig_hash: sigHash },
        required: true,
      });
    ops.push(insert("device_replays", { device_id: id, sig_hash: sigHash, expires_at: expires(600) }));
    try {
      await store.atomic(ops);
    } catch (error) {
      if (error instanceof StoreConflict) throw new GateError(401, "bad_signature");
      throw error;
    }
    return id;
  }
  async function isFollower(provider: Provider, handle: string, xUserId: string | null) {
    if (provider !== "x") return false;
    if (await first(store, "followers", { provider: "x", handle })) return true;
    if (xUserId) {
      const rows = await store.list("followers", { provider: "x" });
      return rows.some((r) => r.x_user_id === xUserId);
    }
    return false;
  }
  async function markFollowFromTable(account: Row, claim: Row) {
    if (account.provider !== "x") return claim;
    if (claim.follow_check === "confirmed") return claim;
    if (await isFollower("x", String(account.handle), account.x_user_id ? String(account.x_user_id) : null)) {
      await store.atomic([
        update("claims", { id: claim.id }, { follow_check: "confirmed" }),
      ]);
      return { ...claim, follow_check: "confirmed" };
    }
    return claim;
  }
  async function expireIfNeeded(claim: Row) {
    if (
      claim.status === "pending_verification" &&
      Date.parse(String(claim.expires_at)) <= Date.now()
    ) {
      await store.atomic([
        update("claims", { id: claim.id, status: "pending_verification" }, { status: "expired" }, true),
      ]);
      return { ...claim, status: "expired" };
    }
    return claim;
  }
  function rebindAvailableAt(binding: Row) {
    return binding.last_rebind_at
      ? new Date(Date.parse(String(binding.last_rebind_at)) + REBIND_COOLDOWN_MS).toISOString()
      : iso();
  }
  async function bindingConflict(accountId: string, deviceId: string) {
    const deviceBound = await first(store, "bindings", { device_id: deviceId });
    if (deviceBound && deviceBound.account_id !== accountId)
      throw new GateError(409, "device_taken");
    const accountBound = await first(store, "bindings", { account_id: accountId });
    if (accountBound && accountBound.device_id !== deviceId) {
      throw new GateError(409, "device_conflict", {
        rebind_available_at: rebindAvailableAt(accountBound),
      });
    }
    return accountBound;
  }
  async function rebindPostRequired(
    accountId: string,
    postKey: string | null,
    postCreatedMs: number,
  ) {
    const binding = await first(store, "bindings", { account_id: accountId });
    if (!binding) return;
    if (binding.device_id) {
      const cooldownEnd =
        Date.parse(String(binding.last_rebind_at ?? binding.bound_at)) + REBIND_COOLDOWN_MS;
      if (cooldownEnd > Date.now()) throw new GateError(429, "rebind_cooldown", {
        rebind_available_at: new Date(cooldownEnd).toISOString(),
      });
    }
    if (!postKey) throw new GateError(422, "post_missing_tag");
    const used = await first(store, "used_posts", { post_key: postKey });
    if (used) throw new GateError(422, "post_url_used");
    const minMs = Date.parse(String(binding.bound_at));
    if (postCreatedMs < minMs) throw new GateError(422, "post_too_old");
  }
  async function tryUnlock(claim: Row, account: Row) {
    claim = await expireIfNeeded(claim);
    if (claim.status !== "pending_verification") return claim;
    claim = await markFollowFromTable(account, claim);
    const needFollow = account.provider === "x";
    if (needFollow && claim.follow_check !== "confirmed") return claim;
    if (claim.post_check !== "confirmed") return claim;
    if (!claim.post_key) return claim;
    await checkRevoked(String(account.id));
    const deviceBound = await first(store, "bindings", { device_id: claim.device_id });
    if (deviceBound && deviceBound.account_id !== account.id) return claim;
    const accountBound = await first(store, "bindings", { account_id: account.id });
    if (accountBound && accountBound.device_id !== claim.device_id) {
      const cooldownEnd =
        Date.parse(String(accountBound.last_rebind_at ?? accountBound.bound_at)) +
        REBIND_COOLDOWN_MS;
      if (cooldownEnd > Date.now()) return claim;
      const used = await first(store, "used_posts", { post_key: String(claim.post_key) });
      if (used) return claim;
      const postMs =
        account.provider === "x" && claim.post_url
          ? snowflakeMs(parseXStatusUrl(String(claim.post_url)).statusId)
          : Date.parse(String(claim.created_at));
      if (postMs < Date.parse(String(accountBound.bound_at))) return claim;
    } else {
      const taken = await first(store, "bindings", { device_id: claim.device_id });
      if (taken && taken.account_id !== account.id) {
        await store.atomic([
          update(
            "claims",
            { id: claim.id },
            { status: "rejected", reject_reason: "device_taken" },
            true,
          ),
        ]);
        return { ...claim, status: "rejected", reject_reason: "device_taken" };
      }
    }
    const binding = accountBound ?? null;
    const unlockExisting = await first(store, "unlocks", { account_id: account.id });
    const displayName =
      account.provider === "x" ? `@${account.handle}` : String(account.handle);
    const ops: Op[] = [
      update(
        "claims",
        { id: claim.id, status: "pending_verification" },
        { status: "unlocked" },
        true,
      ),
      update(
        "accounts",
        { id: account.id },
        {
          wall_opt_in: claim.show_on_wall === true,
          wall_opt_in_at: claim.show_on_wall === true ? iso() : null,
        },
      ),
      insert(
        "used_posts",
        { post_key: String(claim.post_key), account_id: account.id, created_at: iso() },
        true,
      ),
    ];
    if (!binding)
      ops.push(
        insert("bindings", {
          account_id: account.id,
          device_id: claim.device_id,
          device_pubkey: claim.device_pubkey,
          device_name: claim.device_name,
          bound_at: iso(),
          last_rebind_at: unlockExisting ? iso() : null,
        }),
      );
    else if (binding.device_id !== claim.device_id)
      ops.push(
        insert(
          "moved_devices",
          { device_id: binding.device_id, account_id: account.id },
          true,
        ),
        update(
          "bindings",
          { account_id: account.id },
          {
            device_id: claim.device_id,
            device_pubkey: claim.device_pubkey,
            device_name: claim.device_name,
            bound_at: iso(),
            last_rebind_at: iso(),
          },
          true,
        ),
        { kind: "delete", table: "moved_devices", where: { device_id: claim.device_id } },
      );
    if (!unlockExisting)
      ops.push(
        insert("unlocks", {
          account_id: account.id,
          provider: account.provider,
          post_ref: claim.post_key,
          handle: account.handle,
          display_name: displayName,
          avatar_url: account.avatar_url ?? null,
          profile_url: account.profile_url ?? profileUrl(account.provider as Provider, String(account.handle)),
          created_at: iso(),
        }),
      );
    try {
      await store.atomic(ops);
    } catch (error) {
      if (error instanceof StoreConflict) {
        const used = await first(store, "used_posts", { post_key: String(claim.post_key) });
        if (used && used.account_id !== account.id)
          throw new GateError(422, "post_url_used");
        throw new GateError(409, "device_taken");
      }
      throw error;
    }
    return { ...claim, status: "unlocked" };
  }
  async function claimResponse(claim: Row, account: Row) {
    claim = await expireIfNeeded(claim);
    if (claim.status === "unlocked") {
      const binding = await first(store, "bindings", { account_id: account.id });
      const unlock = await first(store, "unlocks", { account_id: account.id });
      if (binding && unlock && binding.device_id === claim.device_id)
        return {
          status: "unlocked",
          checks: checksPayload(claim),
          reason: null,
          entitlement: token(binding, unlock, account),
        };
    }
    return {
      status: claim.status,
      checks: checksPayload(claim),
      reason: claim.reject_reason ? String(claim.reject_reason) : null,
      entitlement: null,
    };
  }
  async function checkInstantPost(
    provider: Provider,
    handle: string,
    postUrl: string,
    claimCreatedMs: number,
    accountId: string,
  ) {
    if (provider === "x") {
      try {
        const parsed = parseXStatusUrl(postUrl);
        if (parsed.handle !== handle)
          return { post_key: parsed.postKey, post_check: "failed" as const, post_source: null };
        const binding = await first(store, "bindings", { account_id: accountId });
        const minMs = binding ? Date.parse(String(binding.bound_at)) : 0;
        const result = await oembed.fetch(postUrl, handle, claimCreatedMs, minMs);
        if (!result.ok) {
          const check =
            result.code === "post_not_found" ? ("pending" as const) : ("failed" as const);
          return { post_key: parsed.postKey, post_check: check, post_source: null };
        }
        const used = await first(store, "used_posts", { post_key: result.postKey });
        if (used && used.account_id !== accountId)
          throw new GateError(422, "post_url_used");
        if (binding)
          await rebindPostRequired(accountId, result.postKey, result.createdMs);
        return {
          post_key: result.postKey,
          post_check: "confirmed" as const,
          post_source: "oembed",
        };
      } catch (error) {
        if (error instanceof GateError) throw error;
        return { post_key: null, post_check: "pending" as const, post_source: null };
      }
    }
    const postKey = validateLinkedInPostUrl(postUrl);
    const used = await first(store, "used_posts", { post_key: postKey });
    if (used && used.account_id !== accountId) throw new GateError(422, "post_url_used");
    const page = await fetchLinkedInPublicPost(postUrl, handle, pageFetch);
    if (!page.ok) return { post_key: postKey, post_check: "pending" as const, post_source: null };
    return { post_key: page.postKey, post_check: "confirmed" as const, post_source: "public" };
  }
  async function upsertAccount(provider: Provider, handle: string) {
    let account = await first(store, "accounts", { provider, handle });
    if (account) return account;
    const row = {
      id: randomUUID(),
      provider,
      handle,
      x_user_id: null,
      verified: false,
      verified_type: null,
      followers_count: null,
      avatar_url: null,
      profile_url: profileUrl(provider, handle),
      wall_opt_in: false,
      wall_opt_in_at: null,
      created_at: iso(),
    };
    await store.atomic([insert("accounts", row)]);
    return row;
  }
  async function recordClaimDevice(deviceId: string) {
    const day = iso().slice(0, 10);
    await store.atomic([
      { kind: "increment", table: "claim_device_limits", where: { device_id: deviceId, day } },
    ]);
    const row = await first(store, "claim_device_limits", { device_id: deviceId, day });
    if (row && Number(row.count) > 10) rateLimited(3600);
  }
  async function pushFollowers(body: Record<string, unknown>) {
    const followers = body.followers;
    if (!Array.isArray(followers)) throw new GateError(400, "invalid_request");
    let upserted = 0;
    const ops: Op[] = [];
    for (const item of followers) {
      const row = object(item);
      const handle = String(row.handle || "").replace(/^@/, "").toLowerCase();
      if (!/^[a-z0-9_]{1,15}$/.test(handle)) continue;
      ops.push(
        insert(
          "followers",
          {
            provider: "x",
            handle,
            x_user_id: row.id ? String(row.id) : null,
          },
          true,
        ),
      );
      upserted += 1;
      const accounts = await store.list("accounts", { provider: "x", handle });
      for (const account of accounts) {
        if (row.id && !account.x_user_id)
          ops.push(update("accounts", { id: account.id }, { x_user_id: String(row.id) }));
      }
    }
    ops.push(
      insert(
        "follower_meta",
        {
          id: "x",
          source: String(body.source || "manual"),
          complete: body.complete === true,
          updated_at: iso(),
        },
        true,
      ),
    );
    if (ops.length) await store.atomic(ops);
    const followConfirmed: string[] = [];
    const unlocked: string[] = [];
    const pending = await store.list("claims", { status: "pending_verification" });
    for (const claim of pending) {
      const account = await first(store, "accounts", { id: claim.account_id });
      if (!account || account.provider !== "x") continue;
      const updated = await markFollowFromTable(account, claim);
      if (updated.follow_check === "confirmed") followConfirmed.push(String(claim.id));
      const after = await tryUnlock(updated, account);
      if (after.status === "unlocked") unlocked.push(String(claim.id));
    }
    return { upserted, follow_confirmed_claims: followConfirmed, unlocked_claims: unlocked };
  }
  async function adminVerifyClaim(claimId: string, payload: Record<string, unknown>) {
    const claim = await first(store, "claims", { id: claimId });
    if (!claim) throw new GateError(404, "not_found");
    let current = await expireIfNeeded(claim);
    if (current.status !== "pending_verification")
      throw new GateError(409, "rejected");
    const account = await first(store, "accounts", { id: claim.account_id });
    if (!account) throw new GateError(404, "not_found");
    const source = payload.source ? String(payload.source) : "manual";
    if (payload.follow_confirmed === true && account.provider === "x") {
      await store.atomic([update("claims", { id: claimId }, { follow_check: "confirmed" })]);
      current = { ...current, follow_check: "confirmed" };
    }
    const postUrl = payload.post_url ? String(payload.post_url) : null;
    const postText = payload.post_text ? String(payload.post_text) : "";
    if (postUrl && account.provider === "x") {
      const handle = String(account.handle);
      if (source === "comet") {
        let httpStatus = 200;
        try {
          const target = new URL(env.OEMBED_BASE || "https://publish.x.com/oembed");
          target.searchParams.set("url", postUrl);
          const response = await fetch(target, { signal: AbortSignal.timeout(8000) });
          httpStatus = response.status;
        } catch {
          httpStatus = 503;
        }
        if (!classifyOEmbedHttpStatus(httpStatus))
          throw new GateError(422, "post_missing_tag");
        if (!matchText(postText, "x")) throw new GateError(422, "post_missing_tag");
        const key = parseXStatusUrl(postUrl).postKey;
        await store.atomic([
          update("claims", { id: claimId }, {
            post_url: postUrl,
            post_key: key,
            post_check: "confirmed",
            post_source: "comet",
          }),
        ]);
        current = { ...current, post_key: key, post_check: "confirmed" };
      } else {
        const result = await oembed.fetch(postUrl, handle, Date.parse(String(current.created_at)));
        if (!result.ok) throw new GateError(422, result.code);
        await store.atomic([
          update("claims", { id: claimId }, {
            post_url: postUrl,
            post_key: result.postKey,
            post_check: "confirmed",
            post_source: "oembed",
          }),
        ]);
        current = { ...current, post_key: result.postKey, post_check: "confirmed" };
      }
    } else if (account.provider === "linkedin" && (postUrl || postText)) {
      if (!matchText(postText, "linkedin")) throw new GateError(422, "post_missing_tag");
      const key = postUrl ? validateLinkedInPostUrl(postUrl) : String(current.post_key || "");
      if (!key) throw new GateError(422, "post_missing_tag");
      await store.atomic([
        update("claims", { id: claimId }, {
          post_url: postUrl ?? current.post_url,
          post_key: key,
          post_check: "confirmed",
          post_source: source,
        }),
      ]);
      current = { ...current, post_key: key, post_check: "confirmed" };
    }
    const accountUpdates: Row = {};
    if (typeof payload.verified === "boolean") accountUpdates.verified = payload.verified;
    if (payload.verified_type != null) accountUpdates.verified_type = String(payload.verified_type);
    if (payload.followers_count != null)
      accountUpdates.followers_count = Number(payload.followers_count);
    const avatar = safeAvatar(payload.avatar_url);
    if (avatar) accountUpdates.avatar_url = avatar;
    if (Object.keys(accountUpdates).length)
      await store.atomic([update("accounts", { id: account.id }, accountUpdates)]);
    const refreshedAccount = { ...account, ...accountUpdates };
    const unlocked = await tryUnlock(current, refreshedAccount);
    return { claim_id: claimId, status: unlocked.status, checks: checksPayload(unlocked) };
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
        .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
        .slice(0, 200)
        .map((u) => ({
          handle: u.handle,
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
    try {
      if (path.startsWith("/v1/public/")) {
        responseHeaders["access-control-allow-origin"] = "*";
        rateLimit("public", 60, 60000, req);
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
      if (path === "/healthz" && method === "GET") return reply(200, { ok: true });
      if (path === "/v1/config" && method === "GET") {
        rateLimit("config", 120, 60000, req);
        return reply(200, config());
      }
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
            { kind: "increment", table: "ref_clicks", where: { ref, day: iso().slice(0, 10) } },
          ]);
        return reply(204, null);
      }
      if (path === "/v1/public/download" && method === "GET") {
        const data = await downloads.get();
        if (!data.url) throw new GateError(503, "server_error");
        await store.atomic([
          { kind: "increment", table: "download_hits", where: { day: iso().slice(0, 10) } },
        ]);
        res.setHeader("access-control-allow-origin", "*");
        return redirect(data.url);
      }
      if (path === "/v1/claims" && method === "POST") {
        rateLimit("claims", 5, 60000, req);
        const raw = await readRawBody(req);
        const data = object(JSON.parse(raw.toString("utf8") || "{}"));
        const provider = text(data.provider, 20) as Provider;
        if (provider !== "x" && provider !== "linkedin")
          throw new GateError(400, "invalid_request");
        const handleRaw = data.handle;
        if (typeof handleRaw !== "string" || !String(handleRaw).trim())
          throw new GateError(400, "invalid_handle");
        const handle = normalizeHandle(provider, text(handleRaw, 200));
        const show = consent(data.show_on_wall, false);
        const pubkey = deviceKey(data.device_pubkey);
        const deviceName = text(data.device_name, 200);
        const postUrl = data.post_url == null ? null : text(data.post_url, 2048);
        const id = await signedRequest(req, "claim", pubkey, raw);
        const account = await upsertAccount(provider, handle);
        const boundDevice = await first(store, "bindings", { device_id: id });
        if (boundDevice) {
          const boundAccount = await first(store, "accounts", {
            id: boundDevice.account_id,
          });
          if (
            !boundAccount ||
            boundAccount.provider !== provider ||
            String(boundAccount.handle) !== handle
          )
            throw new GateError(409, "device_taken");
        }
        const open = (await store.list("claims", {
          account_id: account.id,
          device_id: id,
          status: "pending_verification",
        })).find((c) => Date.parse(String(c.expires_at)) > Date.now());
        if (open) {
          let current = await markFollowFromTable(account, open);
          current = await tryUnlock(current, account);
          const response = await claimResponse(current, account);
          if (response.status === "unlocked")
            return reply(200, { claim_id: open.id, ...response, poll_after: 60 });
          return reply(200, {
            claim_id: open.id,
            status: current.status,
            checks: checksPayload(current),
            poll_after: 60,
          });
        }
        try {
          await bindingConflict(String(account.id), id);
        } catch (error) {
          if (error instanceof GateError && error.code === "device_taken") throw error;
        }
        await recordClaimDevice(id);
        const claimId = randomUUID();
        const createdAt = iso();
        const claimCreatedMs = Date.parse(createdAt);
        let followCheck = provider === "x" ? "pending" : "n/a";
        if (provider === "x" && (await isFollower("x", handle, account.x_user_id ? String(account.x_user_id) : null)))
          followCheck = "confirmed";
        let postCheck = "pending";
        let postKey: string | null = null;
        let postSource: string | null = null;
        if (postUrl) {
          const instant = await checkInstantPost(provider, handle, postUrl, claimCreatedMs, String(account.id));
          postKey = instant.post_key;
          postCheck = instant.post_check;
          postSource = instant.post_source;
        }
        const claimRow = {
          id: claimId,
          account_id: account.id,
          device_id: id,
          device_pubkey: pubkey,
          device_name: deviceName,
          post_url: postUrl,
          post_key: postKey,
          follow_check: followCheck,
          post_check: postCheck,
          status: "pending_verification",
          reject_reason: null,
          show_on_wall: show,
          post_source: postSource,
          created_at: createdAt,
          expires_at: new Date(Date.now() + CLAIM_TTL_MS).toISOString(),
        };
        await store.atomic([insert("claims", claimRow)]);
        let claim = claimRow as Row;
        try {
          claim = await tryUnlock(claim, account);
          if (claim.status === "rejected" && claim.reject_reason === "device_taken")
            return reply(409, { error: "device_taken", message: "device taken" });
        } catch (error) {
          if (error instanceof GateError) {
            const refreshed = await first(store, "claims", { id: claimId });
            if (refreshed?.status === "rejected")
              return reply(409, { error: error.code, message: error.message, ...error.extra });
            throw error;
          }
          throw error;
        }
        const response = await claimResponse(claim, account);
        if (response.status === "unlocked")
          return reply(201, { claim_id: claimId, ...response, poll_after: 60 });
        return reply(201, {
          claim_id: claimId,
          status: claim.status,
          checks: checksPayload(claim),
          poll_after: 60,
        });
      }
      const pollMatch = path.match(/^\/v1\/claims\/([\w-]+)$/);
      if (pollMatch && method === "GET") {
        const raw = Buffer.alloc(0);
        const claim = await first(store, "claims", { id: pollMatch[1] });
        if (!claim) throw new GateError(404, "not_found");
        const id = await signedRequest(req, "poll", String(claim.device_pubkey), raw);
        if (id !== claim.device_id) throw new GateError(401, "bad_signature");
        rateDevice("poll", 60, 3600000, id);
        const account = await first(store, "accounts", { id: claim.account_id });
        if (!account) throw new GateError(404, "not_found");
        let current = await markFollowFromTable(account, claim);
        current = await tryUnlock(current, account);
        return reply(200, await claimResponse(current, account));
      }
      if (path === "/v1/entitlement/refresh" && method === "POST") {
        rateLimit("refresh", 30, 60000, req);
        const raw = await readRawBody(req);
        const data = object(JSON.parse(raw.toString("utf8") || "{}"));
        const id = deviceIdHeader(req.headers["x-acu-device"] as string | undefined);
        if (deviceId(data.device_id) !== id) throw new GateError(401, "bad_signature");
        const binding = await first(store, "bindings", { device_id: id });
        if (!binding) {
          if (await first(store, "moved_devices", { device_id: id }))
            throw new GateError(409, "device_moved");
          throw new GateError(404, "not_found");
        }
        await signedRequest(req, "refresh", String(binding.device_pubkey), raw);
        await checkRevoked(String(binding.account_id));
        const unlock = await first(store, "unlocks", { account_id: binding.account_id });
        if (!unlock) throw new GateError(404, "not_found");
        const account = await first(store, "accounts", { id: binding.account_id });
        return reply(200, { entitlement: token(binding, unlock, account!) });
      }
      if (path === "/v1/wall" && method === "POST") {
        rateLimit("wall", 30, 60000, req);
        const raw = await readRawBody(req);
        const data = object(JSON.parse(raw.toString("utf8") || "{}"));
        const show = consent(data.show);
        const id = deviceId(data.device_id);
        const binding = await first(store, "bindings", { device_id: id });
        if (!binding) throw new GateError(404, "not_found");
        await signedRequest(req, "wall", String(binding.device_pubkey), raw);
        await checkRevoked(String(binding.account_id));
        await store.atomic([
          update(
            "accounts",
            { id: binding.account_id },
            { wall_opt_in: show, wall_opt_in_at: show ? iso() : null },
            true,
          ),
        ]);
        return reply(200, { show });
      }
      const admin = path.startsWith("/v1/admin/");
      if (admin) {
        if (!constantEqual(bearer(req) || "", env.ADMIN_TOKEN!)) adminAuthFail(req);
        if (path === "/v1/admin/claims" && method === "GET") {
          const status = url.searchParams.get("status") || "pending_verification";
          const provider = url.searchParams.get("provider");
          const limit = Math.min(Number(url.searchParams.get("limit") || 200), 500);
          let claims = await store.list("claims", { status });
          if (provider === "x" || provider === "linkedin") {
            const accounts = await store.list("accounts", { provider });
            const ids = new Set(accounts.map((a) => a.id));
            claims = claims.filter((c) => ids.has(c.account_id));
          }
          claims = claims.slice(0, limit);
          const rows = [];
          for (const claim of claims) {
            const account = await first(store, "accounts", { id: claim.account_id });
            rows.push({
              id: claim.id,
              provider: account?.provider,
              handle: account?.handle,
              x_user_id: account?.x_user_id ?? null,
              post_url: claim.post_url,
              checks: checksPayload(claim),
              created_at: claim.created_at,
              expires_at: claim.expires_at,
              device_name: claim.device_name,
            });
          }
          return reply(200, { claims: rows });
        }
        if (path === "/v1/admin/followers" && method === "POST")
          return reply(200, await pushFollowers(await body(req)));
        const verifyMatch = path.match(/^\/v1\/admin\/claims\/([\w-]+)\/verify$/);
        if (verifyMatch && method === "POST")
          return reply(200, await adminVerifyClaim(verifyMatch[1], await body(req)));
        if (path === "/v1/admin/claims/verify-batch" && method === "POST") {
          const payload = await body(req);
          const results = Array.isArray(payload.results) ? payload.results : [];
          const out = [];
          for (const item of results) {
            const row = object(item);
            const claimId = text(row.claim_id);
            try {
              out.push(await adminVerifyClaim(claimId, row));
            } catch (error) {
              out.push({
                claim_id: claimId,
                error: error instanceof GateError ? error.code : "server_error",
              });
            }
          }
          return reply(200, { results: out });
        }
        const rejectMatch = path.match(/^\/v1\/admin\/claims\/([\w-]+)\/reject$/);
        if (rejectMatch && method === "POST") {
          const payload = await body(req);
          const reason = text(payload.reason, 64);
          await store.atomic([
            update(
              "claims",
              { id: rejectMatch[1], status: "pending_verification" },
              { status: "rejected", reject_reason: reason },
              true,
            ),
          ]);
          return reply(200, { status: "rejected", reason });
        }
        const action = path.match(/^\/v1\/admin\/accounts\/([\w-]+)\/(revoke|reset-rebind)$/);
        if (action && method === "POST") {
          if (!(await first(store, "accounts", { id: action[1] })))
            throw new GateError(404, "not_found");
          await store.atomic(
            action[2] === "revoke"
              ? [
                  insert("revocations", { account_id: action[1], created_at: iso() }, true),
                  update("accounts", { id: action[1] }, { wall_opt_in: false, wall_opt_in_at: null }),
                ]
              : [update("bindings", { account_id: action[1] }, { last_rebind_at: null })],
          );
          return reply(200, { ok: true });
        }
        if (path === "/v1/admin/stats" && method === "GET") {
          const stats = await siteStats();
          const unlocks = await store.list("unlocks");
          const accounts = await store.list("accounts");
          const refs = await store.list("ref_clicks");
          const claims = await store.list("claims");
          const verifiedAccounts = accounts.filter((a) => a.verified === true);
          const byType: Record<string, number> = {};
          for (const account of verifiedAccounts) {
            const type = String(account.verified_type || "unknown");
            byType[type] = (byType[type] || 0) + 1;
          }
          const reach = accounts.reduce(
            (sum, a) => sum + Number(a.followers_count || 0),
            0,
          );
          return reply(200, {
            unlocks: {
              x: unlocks.filter((u) => u.provider === "x").length,
              linkedin: unlocks.filter((u) => u.provider === "linkedin").length,
              total: unlocks.length,
            },
            verified_unlocks: verifiedAccounts.length,
            verified_by_type: byType,
            reach,
            claims: {
              pending: claims.filter((c) => c.status === "pending_verification").length,
              rejected: claims.filter((c) => c.status === "rejected").length,
              expired: claims.filter((c) => c.status === "expired").length,
            },
            followers_known: (await store.list("followers")).length,
            ref_clicks: {
              "acu-x": refs
                .filter((r) => r.ref === "acu-x")
                .reduce((n, r) => n + Number(r.count), 0),
              "acu-li": refs
                .filter((r) => r.ref === "acu-li")
                .reduce((n, r) => n + Number(r.count), 0),
            },
            downloads: stats.downloads,
            wall_opt_in: accounts.filter((a) => a.wall_opt_in === true).length,
          });
        }
      }
      throw new GateError(404, "not_found");
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
function deviceId(value: unknown) {
  const id = text(value, 64);
  if (!/^[a-f0-9]{64}$/.test(id)) throw new GateError(400, "invalid_request");
  return id;
}
/** main starts the production gate server. */
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
