import assert from "node:assert/strict";
import { once } from "node:events";
import { generateKeyPairSync, randomBytes, sign } from "node:crypto";
import type { TestContext } from "node:test";
import { createGate } from "../src/server.ts";
import { startMockServices } from "../src/mock-services.ts";
import { MemoryStore } from "../src/store.ts";
import type { Store } from "../src/store.ts";
import { sha256Hex } from "../src/crypto.ts";
import { preset } from "../src/settings.ts";

let signSecond = Math.floor(Date.now() / 1000);

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

export function signRequest(
  d: ReturnType<typeof device>,
  action: "claim" | "poll" | "refresh" | "wall",
  body: string | Record<string, unknown> = "{}",
) {
  const raw = typeof body === "string" ? body : JSON.stringify(body);
  const ts = signSecond++;
  const bodyHash = sha256Hex(raw);
  const message = `acu-gate|${action}|${d.id}|${ts}|${bodyHash}`;
  return {
    headers: {
      "x-acu-device": d.id,
      "x-acu-ts": String(ts),
      "x-acu-sig": sign("sha256", Buffer.from(message), d.privateKey).toString("base64"),
      "content-type": "application/json",
    },
    body: raw,
  };
}

export function goodPostText() {
  return preset.x.preset_text;
}

export function xPostUrl(handle: string, statusId = "1999999999999999999") {
  return `https://x.com/${handle}/status/${statusId}`;
}

export async function harness(
  t: TestContext,
  store: Store = new MemoryStore(),
  envExtra: Record<string, string> = {},
) {
  const mock = await startMockServices();
  const gate = createGate(
    {
      DEV: "1",
      PUBLIC_BASE_URL: "http://127.0.0.1",
      ...mock.env,
      ...envExtra,
    },
    store,
    { pageFetch: mock.pageFetch, oembed: undefined, downloads: undefined },
  );
  gate.server.listen(0, "127.0.0.1");
  await once(gate.server, "listening");
  const address = gate.server.address();
  if (!address || typeof address === "string") throw new Error("listen_failed");
  const base = `http://127.0.0.1:${address.port}`;
  gate.env.PUBLIC_BASE_URL = base;
  t.after(async () => {
    await mock.close();
    await new Promise<void>((resolve, reject) =>
      gate.server.close((error) => (error ? reject(error) : resolve())),
    );
  });
  async function claim(
    provider: "x" | "linkedin",
    handle: string,
    d = device(),
    post_url: string | null = null,
    show_on_wall = false,
  ) {
    const payload = {
      provider,
      handle,
      post_url,
      device_pubkey: d.key,
      device_name: "Test Mac",
      show_on_wall,
    };
    const signed = signRequest(d, "claim", payload);
    const response = await fetch(`${base}/v1/claims`, {
      method: "POST",
      headers: signed.headers,
      body: signed.body,
    });
    const data = await response.json();
    return { status: response.status, data, device: d };
  }
  async function poll(claimId: string, d: ReturnType<typeof device>) {
    const signed = signRequest(d, "poll", "");
    const response = await fetch(`${base}/v1/claims/${claimId}`, {
      method: "GET",
      headers: signed.headers,
    });
    return { status: response.status, data: await response.json() };
  }
  async function request(
    path: string,
    options: {
      method?: string;
      data?: unknown;
      admin?: boolean;
      headers?: Record<string, string>;
      body?: string;
    } = {},
  ) {
    const response = await fetch(base + path, {
      method: options.method || (options.data === undefined && !options.body ? "GET" : "POST"),
      redirect: "manual",
      headers: {
        ...(options.admin
          ? { authorization: `Bearer ${gate.env.ADMIN_TOKEN}` }
          : {}),
        ...(options.headers ?? {}),
        ...(options.body !== undefined ||
        (options.data !== undefined &&
          (options.method === "POST" || options.method === "PUT" || options.method === "PATCH"))
          ? { "content-type": "application/json" }
          : {}),
      },
      ...(options.body !== undefined
        ? { body: options.body }
        : options.data !== undefined &&
            (options.method === "POST" ||
              options.method === "PUT" ||
              options.method === "PATCH" ||
              (!options.method && options.data !== undefined))
          ? { body: JSON.stringify(options.data) }
          : {}),
    });
    let data: Record<string, unknown> = {};
    if (
      response.status !== 204 &&
      response.headers.get("content-type")?.includes("application/json")
    )
      data = await response.json();
    else await response.arrayBuffer();
    return { status: response.status, data, headers: response.headers };
  }
  return { mock, gate, store, base, request, claim, poll, device };
}
