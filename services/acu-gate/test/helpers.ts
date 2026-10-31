import assert from "node:assert/strict";
import { once } from "node:events";
import { generateKeyPairSync, randomBytes, sign } from "node:crypto";
import type { TestContext } from "node:test";
import { createGate } from "../src/server.ts";
import { startMocks, closeServer } from "../src/mock-providers.ts";
import { MemoryStore } from "../src/store.ts";
import type { Store } from "../src/store.ts";
import { pkce, opaque } from "../src/providers.ts";
export function device() {
  const keys = generateKeyPairSync("ec", { namedCurve: "prime256v1" }),
    jwk = keys.publicKey.export({ format: "jwk" });
  return {
    id: randomBytes(32).toString("hex"),
    key: Buffer.concat([
      Buffer.from([4]),
      Buffer.from(jwk.x!, "base64url"),
      Buffer.from(jwk.y!, "base64url"),
    ]).toString("base64"),
    privateKey: keys.privateKey,
  };
}
export function signature(
  d: ReturnType<typeof device>,
  action: string,
  ts = Math.floor(Date.now() / 1000),
  show?: boolean,
) {
  const message = `acu-gate-${action}|${d.id}|${ts}${action === "wall" ? `|${show}` : ""}`;
  return {
    device_id: d.id,
    ts,
    sig: sign("sha256", Buffer.from(message), d.privateKey).toString("base64"),
    ...(show === undefined ? {} : { show }),
  };
}
export function callbackParams(location: string) {
  const target = new URL(location);
  return target.hash.startsWith("#")
    ? new URLSearchParams(target.hash.slice(1))
    : target.searchParams;
}
export async function harness(
  t: TestContext,
  store: Store = new MemoryStore(),
  envExtra: Record<string, string> = {},
) {
  const mock = await startMocks();
  const gate = createGate(
    {
      DEV: "1",
      PUBLIC_BASE_URL: "http://127.0.0.1",
      OWNER_X_USER_ID: "owner",
      FOLLOW_CHECK_MODE: "user_following",
      ...mock.env,
      ...envExtra,
    },
    store,
    { pageFetch: mock.pageFetch },
  );
  gate.server.listen(0, "127.0.0.1");
  await once(gate.server, "listening");
  const address = gate.server.address();
  if (!address || typeof address === "string") throw new Error("listen_failed");
  const base = `http://127.0.0.1:${address.port}`;
  gate.env.PUBLIC_BASE_URL = base;
  t.after(async () => {
    await closeServer(gate.server);
    await mock.close();
  });
  async function request(
    path: string,
    options: {
      method?: string;
      data?: unknown;
      session?: string;
      admin?: boolean;
    } = {},
  ) {
    const response = await fetch(base + path, {
      method: options.method || (options.data === undefined ? "GET" : "POST"),
      redirect: "manual",
      headers: {
        ...(options.session
          ? { authorization: `Bearer ${options.session}` }
          : {}),
        ...(options.admin
          ? { authorization: `Bearer ${gate.env.ADMIN_TOKEN}` }
          : {}),
        ...(options.data === undefined
          ? {}
          : { "content-type": "application/json" }),
      },
      ...(options.data === undefined
        ? {}
        : { body: JSON.stringify(options.data) }),
    });
    let data: Record<string, any> = {};
    if (
      response.status !== 204 &&
      response.headers.get("content-type")?.includes("application/json")
    )
      data = await response.json();
    else await response.arrayBuffer();
    return { status: response.status, data, headers: response.headers };
  }
  async function linkTicket(session: string, deviceId: string) {
    const ticket = await request("/v1/auth/link-ticket", {
      data: { device_id: deviceId },
      session,
    });
    assert.equal(ticket.status, 200);
    return String(ticket.data.ticket);
  }
  async function authorize(
    provider = "x",
    user = "guru_test",
    d = device(),
    link?: string,
  ) {
    const verifier = opaque();
    const params = new URLSearchParams({
      device_id: d.id,
      device_name: "Test Mac",
      app_challenge: pkce(verifier),
      mock_user: user,
    });
    if (link) {
      params.set("link", "1");
      params.set("link_ticket", await linkTicket(link, d.id));
    }
    const start = await request(`/v1/auth/${provider}/start?${params}`);
    assert.equal(start.status, 302);
    const authorize = new URL(start.headers.get("location")!);
    authorize.searchParams.set("mock_user", user);
    const approved = await fetch(authorize, { redirect: "manual" });
    assert.equal(approved.status, 302);
    await approved.arrayBuffer();
    const callback = await fetch(approved.headers.get("location")!, {
      redirect: "manual",
    });
    assert.equal(callback.status, 302);
    await callback.arrayBuffer();
    const location = callback.headers.get("location")!;
    const callbackQuery = callbackParams(location);
    assert.equal(callbackQuery.get("error"), null, "OAuth callback failed");
    assert.match(location, /#code=/);
    return { code: callbackQuery.get("code"), verifier, device: d };
  }
  async function login(
    provider = "x",
    user = "guru_test",
    d = device(),
    link?: string,
  ) {
    const prepared = await authorize(provider, user, d, link);
    const redeemed = await request("/v1/session/redeem", {
      data: {
        code: prepared.code,
        app_verifier: prepared.verifier,
        device_id: d.id,
        device_pubkey: d.key,
      },
    });
    return { ...redeemed, ...prepared };
  }
  return { mock, gate, store, base, request, login, authorize };
}
