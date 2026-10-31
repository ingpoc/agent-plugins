#!/usr/bin/env node
import { readFileSync, existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";

function usage() {
  process.stderr.write(`Usage:
  gate-admin.mjs [--wake] list [--provider x|linkedin]
  gate-admin.mjs [--wake] push-followers <file.json>
  gate-admin.mjs [--wake] verify <claim_id> [--post-url U] [--post-text T] [--follow] [--source comet]
    [--post-ok | --post-not-ok] [--verified] [--followers-count N] [--avatar-url U]
  gate-admin.mjs [--wake] verify-batch <file.json>
  gate-admin.mjs [--wake] reject <claim_id> <reason>
  gate-admin.mjs [--wake] stats

Base URL: --base, ACU_GATE_BASE_URL, or ~/.config/acu-gate/gate.json {"base_url": "..."}
Admin token: ACU_GATE_ADMIN_TOKEN_FILE, else macOS Keychain service acu-gate / account admin_token
--wake: retry up to 90s on network failure or HTTP 502/503/504 (Render free cold start)
`);
}

function parseArgs(argv) {
  const out = { flags: {}, rest: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--base") {
      out.flags.base = argv[++i];
      continue;
    }
    if (arg === "--post-ok" || arg === "--post-not-ok") {
      out.flags[arg.slice(2).replace(/-/g, "_")] = true;
      continue;
    }
    if (arg.startsWith("--")) {
      const key = arg.slice(2).replace(/-/g, "_");
      const next = argv[i + 1];
      if (next && !next.startsWith("--")) {
        out.flags[key] = next;
        i += 1;
      } else out.flags[key] = true;
      continue;
    }
    out.rest.push(arg);
  }
  return out;
}

function gateJsonBase() {
  const path = join(homedir(), ".config", "acu-gate", "gate.json");
  if (!existsSync(path)) return null;
  const data = JSON.parse(readFileSync(path, "utf8"));
  return typeof data.base_url === "string" ? data.base_url.replace(/\/$/, "") : null;
}

function resolveBase(flags) {
  if (flags.base) return String(flags.base).replace(/\/$/, "");
  if (process.env.ACU_GATE_BASE_URL)
    return process.env.ACU_GATE_BASE_URL.replace(/\/$/, "");
  return gateJsonBase();
}

function adminToken() {
  const file = process.env.ACU_GATE_ADMIN_TOKEN_FILE;
  if (file) return readFileSync(file, "utf8").trim();
  if (process.platform === "darwin") {
    return execFileSync(
      "security",
      ["find-generic-password", "-s", "acu-gate", "-a", "admin_token", "-w"],
      { encoding: "utf8" },
    ).trim();
  }
  throw new Error("Set ACU_GATE_ADMIN_TOKEN_FILE or store token in Keychain on macOS");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function retryable(error) {
  if (error?.cause?.code === "ECONNREFUSED" || error?.cause?.code === "ENOTFOUND")
    return true;
  if (error?.name === "TypeError" && String(error.message).includes("fetch"))
    return true;
  const status = error?.status;
  return status === 502 || status === 503 || status === 504;
}

async function api(base, token, path, method, body) {
  const response = await fetch(`${base}${path}`, {
    method,
    headers: {
      authorization: `Bearer ${token}`,
      ...(body ? { "content-type": "application/json" } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const text = await response.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }
  }
  if (!response.ok) {
    const error = new Error(String(data.error || response.status));
    error.data = data;
    error.status = response.status;
    throw error;
  }
  return data;
}

async function apiWake(wake, base, token, path, method, body) {
  const deadline = wake ? Date.now() + 90000 : Date.now();
  for (;;) {
    try {
      return await api(base, token, path, method, body);
    } catch (error) {
      if (!wake || !retryable(error) || Date.now() >= deadline) throw error;
      await sleep(2000);
    }
  }
}

const { flags, rest } = parseArgs(process.argv.slice(2));
const cmd = rest[0];
const wake = flags.wake === true;
const base = resolveBase(flags);
if (!base) {
  process.stderr.write(
    "Set --base, ACU_GATE_BASE_URL, or ~/.config/acu-gate/gate.json with base_url\n",
  );
  process.exitCode = 1;
} else {
  try {
    const token = adminToken();
    const call = (...args) => apiWake(wake, base, token, ...args);
    let result;
    if (cmd === "list") {
      const q = new URLSearchParams({ status: "pending_verification" });
      if (flags.provider) q.set("provider", String(flags.provider));
      result = await call(`/v1/admin/claims?${q}`, "GET");
    } else if (cmd === "push-followers") {
      const payload = JSON.parse(readFileSync(rest[1], "utf8"));
      result = await call("/v1/admin/followers", "POST", payload);
    } else if (cmd === "verify") {
      if (flags.post_ok && flags.post_not_ok) throw new Error("Choose --post-ok or --post-not-ok");
      const body = { source: flags.source || "manual" };
      if (flags.post_ok) body.post_ok = true;
      if (flags.post_not_ok) body.post_ok = false;
      if (flags.post_url) body.post_url = flags.post_url;
      if (flags.post_text) body.post_text = flags.post_text;
      if (flags.follow) body.follow_confirmed = true;
      if (flags.verified) body.verified = true;
      if (flags.followers_count) body.followers_count = Number(flags.followers_count);
      if (flags.avatar_url) body.avatar_url = flags.avatar_url;
      result = await call(`/v1/admin/claims/${rest[1]}/verify`, "POST", body);
    } else if (cmd === "verify-batch") {
      const payload = JSON.parse(readFileSync(rest[1], "utf8"));
      result = await call("/v1/admin/claims/verify-batch", "POST", payload);
    } else if (cmd === "reject") {
      result = await call(`/v1/admin/claims/${rest[1]}/reject`, "POST", {
        reason: rest[2] || "other",
      });
    } else if (cmd === "stats") {
      result = await call("/v1/admin/stats", "GET");
    } else {
      usage();
      process.exitCode = 1;
    }
    if (result) process.stdout.write(`${JSON.stringify(result)}\n`);
  } catch (error) {
    process.stderr.write(`${JSON.stringify(error.data || { error: error.message })}\n`);
    process.exitCode = 1;
  }
}
