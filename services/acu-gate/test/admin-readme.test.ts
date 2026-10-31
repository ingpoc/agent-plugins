import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ADMIN_ROUTES = [
  { method: "GET", path: "/v1/admin/claims", serverNeedle: '"/v1/admin/claims"' },
  { method: "POST", path: "/v1/admin/followers", serverNeedle: "/v1/admin/followers" },
  {
    method: "POST",
    path: "/v1/admin/claims/{id}/verify",
    serverNeedle: "/v1/admin/claims/",
    serverAlso: "/verify",
  },
  {
    method: "POST",
    path: "/v1/admin/claims/verify-batch",
    serverNeedle: "/v1/admin/claims/verify-batch",
  },
  {
    method: "POST",
    path: "/v1/admin/claims/{id}/reject",
    serverNeedle: "/v1/admin/claims/",
    serverAlso: "/reject",
  },
  { method: "GET", path: "/v1/admin/stats", serverNeedle: "/v1/admin/stats" },
  {
    method: "POST",
    path: "/v1/admin/accounts/{id}/revoke",
    serverNeedle: "admin\\/accounts",
    serverAlso: "revoke",
  },
  {
    method: "POST",
    path: "/v1/admin/accounts/{id}/reset-rebind",
    serverNeedle: "admin\\/accounts",
    serverAlso: "reset-rebind",
  },
];

test("server.ts admin routes are documented in README", () => {
  const server = readFileSync(join(process.cwd(), "src/server.ts"), "utf8");
  const readme = readFileSync(join(process.cwd(), "README.md"), "utf8");
  const section = readme.includes("## Admin API for the verification routine")
    ? readme
        .split("## Admin API for the verification routine")[1]
        ?.split("\n## Public HTTP")[0] ?? ""
    : "";
  assert.ok(section.length, "Admin API section missing");
  for (const route of ADMIN_ROUTES) {
    assert.ok(server.includes(route.serverNeedle), `server missing ${route.path}`);
    if (route.serverAlso) assert.ok(server.includes(route.serverAlso));
    const doc = `\`${route.method} ${route.path}\``;
    assert.ok(section.includes(doc), `README missing ${doc}`);
  }
});
