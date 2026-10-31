import assert from "node:assert/strict";
import { test } from "node:test";
import type { IncomingMessage } from "node:http";
import { clientIp } from "../src/client-ip.ts";

function req(forwarded: string | undefined, remote = "10.0.0.9") {
  return {
    headers: forwarded ? { "x-forwarded-for": forwarded } : {},
    socket: { remoteAddress: remote },
  } as IncomingMessage;
}

test("clientIp uses socket address when proxy hops are zero", () => {
  assert.equal(clientIp(req("203.0.113.1, 198.51.100.2"), 0), "10.0.0.9");
});

test("clientIp trusts X-Forwarded-For hop count", () => {
  assert.equal(
    clientIp(req("203.0.113.1, 198.51.100.2, 192.0.2.44"), 1),
    "192.0.2.44",
  );
  assert.equal(
    clientIp(req("203.0.113.1, 198.51.100.2, 192.0.2.44"), 2),
    "198.51.100.2",
  );
});
