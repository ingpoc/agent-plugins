import test from "node:test";
import assert from "node:assert/strict";
import { createPrivateKey, createPublicKey, verify } from "node:crypto";
import { config, matchesPreset } from "../src/settings.ts";
import { harness, device, signRequest, goodPostText, xPostUrl } from "./helpers.ts";
import { seedGoodPost } from "../src/mock-services.ts";
import { MemoryStore } from "../src/store.ts";

function entitlementClaims(token: string, privateKey: string) {
  const [payload, signature] = token.split(".");
  const publicKey = createPublicKey(createPrivateKey({
    key: Buffer.from(privateKey, "base64"), format: "der", type: "pkcs8",
  }));
  assert.equal(verify(null, Buffer.from(payload), publicKey, Buffer.from(signature, "base64url")), true);
  return JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
}

function assertRollingTrialToken(token: string, privateKey: string, endsAt: string) {
  const claims = entitlementClaims(token, privateKey);
  assert.equal(claims.tier, "trial");
  assert.equal(claims.exp, Math.floor(Date.parse(endsAt) / 1000));
  return claims;
}

test("launch config validates environment overrides and publishes repost targets", () => {
  const value = config({
    GATE_X_LAUNCH_POST_ID: "1234567890",
    GATE_LINKEDIN_LAUNCH_POST_URL:
      "https://www.linkedin.com/posts/owner_launch-activity-1234567890",
  });
  assert.equal(value.x.launch_post_id, "1234567890");
  assert.equal(value.x.repost_url, "https://x.com/intent/retweet?tweet_id=1234567890");
  assert.equal(
    value.linkedin.launch_post_url,
    "https://www.linkedin.com/posts/owner_launch-activity-1234567890",
  );
  assert.equal(value.linkedin.repost_url, value.linkedin.launch_post_url);
  assert.throws(() => config({ GATE_X_LAUNCH_POST_ID: "javascript:bad" }), /Invalid x launch post configuration/);
  assert.throws(() => config({ GATE_LINKEDIN_LAUNCH_POST_URL: "https://example.com/launch" }), /Invalid linkedin launch post configuration/);
});

test("preset matching recognizes both provider domains and rejects cross-provider refs", () => {
  assert.equal(matchesPreset("hello https://acu-dynamic-island.netlify.app/?ref=acu-x", "x"), true);
  assert.equal(matchesPreset("hello https://acu-dynamic-island.netlify.app/?ref=acu-li", "linkedin"), true);
  assert.equal(matchesPreset("https://acu-dynamic-island.netlify.app/?ref=acu-li", "x"), false);
  assert.equal(matchesPreset("hello https://ingpoc-agent-plugins.rpg-ramashram.chatgpt.site/?ref=acu-x", "x"), true);
  assert.equal(matchesPreset("hello https://ingpoc-agent-plugins.nicewasp128.chatgpt.site/?ref=acu-li", "linkedin"), true);
  assert.equal(matchesPreset("https://ingpoc-agent-plugins.rpg-ramashram.chatgpt.site/?ref=acu-li", "x"), false);
  assert.equal(matchesPreset("https://ingpoc-agent-plugins.nicewasp128.chatgpt.site/?ref=acu-x", "linkedin"), false);
});

test("LinkedIn accepts vanity/profile URLs, creates a trial immediately, and omits post_url", async (t) => {
  const h = await harness(t);
  for (const handle of ["alice", "https://www.linkedin.com/in/bobby/", "https://ca.linkedin.com/in/cindy/"]) {
    const created = await h.claim("linkedin", handle);
    assert.equal(created.status, 201);
    assert.equal(created.data.status, "trial");
    assert.ok(created.data.trial_ends_at);
    assert.ok(created.data.entitlement);
    const trial = (await h.store.list("trials", { claim_id: created.data.claim_id }))[0];
    assertRollingTrialToken(created.data.entitlement, h.gate.env.ENTITLEMENT_PRIVATE_KEY!, trial.ends_at);
    const row = (await h.store.list("claims", { id: created.data.claim_id }))[0];
    assert.equal(row.post_url, null);
  }
});

test("LinkedIn vanity/profile lengths enforce 3 to 100 characters", async (t) => {
  const h = await harness(t);
  for (const handle of ["ab", "a".repeat(101)]) {
    const result = await h.claim("linkedin", handle);
    assert.equal(result.status, 400);
  }
});

test("unconfigured launches keep both providers on rolling signed trials across claim, poll, and refresh", async (t) => {
  const h = await harness(t);
  const cases = [
    { provider: "linkedin" as const, handle: "rollingli" },
    { provider: "x" as const, handle: "rollingx" },
  ];
  await h.store.atomic([
    { kind: "insert", table: "followers", row: { provider: "x", handle: "rollingx", x_user_id: "82" } },
  ]);

  for (const { provider, handle } of cases) {
    const created = await h.claim(provider, handle);
    assert.equal(created.status, 201);
    assert.equal(created.data.status, "trial");
    assert.equal(created.data.poll_after, 300);
    let trial = (await h.store.list("trials", { claim_id: created.data.claim_id }))[0];
    assert.ok(Math.abs(Date.parse(trial.ends_at) - Date.now() - 72 * 3600 * 1000) < 5000);
    assertRollingTrialToken(created.data.entitlement, h.gate.env.ENTITLEMENT_PRIVATE_KEY!, trial.ends_at);

    await h.store.atomic([{ kind: "update", table: "claims", where: { id: created.data.claim_id }, row: { status: "expired", expires_at: new Date(Date.now() - 1000).toISOString() } }]);
    await h.store.atomic([{ kind: "update", table: "trials", where: { id: trial.id }, row: { ends_at: new Date(Date.now() - 1000).toISOString() } }]);
    const polled = await h.poll(String(created.data.claim_id), created.device);
    assert.equal(polled.data.status, "trial");
    trial = (await h.store.list("trials", { id: trial.id }))[0];
    assert.ok(Math.abs(Date.parse(trial.ends_at) - Date.now() - 72 * 3600 * 1000) < 5000);
    assertRollingTrialToken(polled.data.entitlement, h.gate.env.ENTITLEMENT_PRIVATE_KEY!, trial.ends_at);
    const claimAfterPoll = (await h.store.list("claims", { id: created.data.claim_id }))[0];
    assert.ok(Date.parse(String(claimAfterPoll.expires_at)) > Date.now() + 29 * 86400 * 1000);

    await h.store.atomic([
      { kind: "update", table: "claims", where: { id: created.data.claim_id }, row: { status: "expired", expires_at: new Date(Date.now() - 1000).toISOString() } },
      { kind: "update", table: "trials", where: { id: trial.id }, row: { ends_at: new Date(Date.now() - 1000).toISOString() } },
    ]);
    const signed = signRequest(created.device, "refresh", { device_id: created.device.id });
    const response = await fetch(`${h.base}/v1/entitlement/refresh`, { method: "POST", headers: signed.headers, body: signed.body });
    assert.equal(response.status, 200);
    const refreshed = await response.json();
    assert.equal(refreshed.status, "trial");
    trial = (await h.store.list("trials", { id: trial.id }))[0];
    assert.ok(Math.abs(Date.parse(trial.ends_at) - Date.now() - 72 * 3600 * 1000) < 5000);
    assertRollingTrialToken(refreshed.entitlement, h.gate.env.ENTITLEMENT_PRIVATE_KEY!, trial.ends_at);
    const claimAfterRefresh = (await h.store.list("claims", { id: created.data.claim_id }))[0];
    assert.ok(Date.parse(String(claimAfterRefresh.expires_at)) > Date.now() + 29 * 86400 * 1000);
  }
});

test("launch first-seen deadlines persist per provider and never shorten a longer trial", async (t) => {
  const prelaunch = await harness(t);
  await prelaunch.store.atomic([
    { kind: "insert", table: "followers", row: { provider: "x", handle: "launchx", x_user_id: "91" } },
  ]);
  const xClaim = await prelaunch.claim("x", "launchx");
  const liClaim = await prelaunch.claim("linkedin", "launchli");
  const xAccount = (await prelaunch.store.list("accounts", { provider: "x", handle: "launchx" }))[0];
  const liAccount = (await prelaunch.store.list("accounts", { provider: "linkedin", handle: "launchli" }))[0];
  let xTrial = (await prelaunch.store.list("trials", { account_id: xAccount.id }))[0];
  let liTrial = (await prelaunch.store.list("trials", { account_id: liAccount.id }))[0];
  const xLongEnd = new Date(Date.now() + 100 * 3600 * 1000).toISOString();
  const liPastEnd = new Date(Date.now() - 1000).toISOString();
  await prelaunch.store.atomic([
    { kind: "update", table: "trials", where: { id: xTrial.id }, row: { ends_at: xLongEnd } },
    { kind: "update", table: "trials", where: { id: liTrial.id }, row: { ends_at: liPastEnd } },
  ]);

  const xLaunched = await harness(t, prelaunch.store, { GATE_X_LAUNCH_POST_ID: "192837465" });
  await xLaunched.request("/v1/config");
  const xSetting = (await prelaunch.store.list("gate_settings", { key: "x_launch_first_seen_at" }))[0];
  assert.ok(xSetting);
  assert.equal(new Date(String(xSetting.value)).toISOString(), xSetting.value);
  assert.equal(new Date(String(xSetting.updated_at)).toISOString(), xSetting.updated_at);
  assert.equal((await prelaunch.store.list("gate_settings", { key: "linkedin_launch_first_seen_at" })).length, 0);
  xTrial = (await prelaunch.store.list("trials", { id: xTrial.id }))[0];
  assert.equal(xTrial.ends_at, xLongEnd);
  assert.equal(xTrial.converted_at, null);

  const bothLaunched = await harness(t, prelaunch.store, {
    GATE_X_LAUNCH_POST_ID: "192837465",
    GATE_LINKEDIN_LAUNCH_POST_URL: "https://www.linkedin.com/posts/owner_launch-activity-1234567890",
  });
  await bothLaunched.request("/v1/config");
  const liSetting = (await prelaunch.store.list("gate_settings", { key: "linkedin_launch_first_seen_at" }))[0];
  assert.ok(liSetting);
  assert.equal(new Date(String(liSetting.value)).toISOString(), liSetting.value);
  assert.equal(new Date(String(liSetting.updated_at)).toISOString(), liSetting.updated_at);
  const expectedLiEnd = Math.max(Date.parse(liPastEnd), Date.parse(String(liSetting.value)) + 72 * 3600 * 1000);
  liTrial = (await prelaunch.store.list("trials", { id: liTrial.id }))[0];
  assert.equal(Date.parse(String(liTrial.ends_at)), expectedLiEnd);
  assert.equal(liTrial.converted_at, null);

  const restarted = await harness(t, prelaunch.store, {
    GATE_X_LAUNCH_POST_ID: "192837465",
    GATE_LINKEDIN_LAUNCH_POST_URL: "https://www.linkedin.com/posts/owner_launch-activity-1234567890",
  });
  await restarted.request("/v1/config");
  assert.equal((await prelaunch.store.list("gate_settings", { key: "x_launch_first_seen_at" }))[0].value, xSetting.value);
  assert.equal((await prelaunch.store.list("gate_settings", { key: "linkedin_launch_first_seen_at" }))[0].value, liSetting.value);
  assert.equal((await prelaunch.store.list("trials", { id: xTrial.id }))[0].ends_at, xLongEnd);
  assert.equal((await prelaunch.store.list("trials", { id: liTrial.id }))[0].ends_at, liTrial.ends_at);
  assert.equal(xClaim.status, 201);
  assert.equal(liClaim.status, 201);
});

test("X trial starts after follow and repost snapshots; repost can convert after expiry", async (t) => {
  const h = await harness(t, undefined, { GATE_X_LAUNCH_POST_ID: "123456789" });
  const created = await h.claim("x", "fanx");
  assert.equal(created.status, 201);
  assert.equal(created.data.status, "pending_verification");
  const follow = await h.request("/v1/admin/followers", {
    admin: true,
    data: { provider: "x", followers: [{ id: "42", handle: "fanx" }], source: "x_plugin", complete: true },
  });
  assert.equal(follow.status, 200);
  const xAccount = (await h.store.list("accounts", { provider: "x", handle: "fanx" }))[0];
  let trial = (await h.store.list("trials", { account_id: xAccount.id }))[0];
  assert.ok(trial);
  const polledTrial = await h.poll(String(created.data.claim_id), created.device);
  assert.equal(polledTrial.data.status, "trial");
  assert.equal(polledTrial.data.poll_after, 300);
  const trialClaims = JSON.parse(Buffer.from(String(polledTrial.data.entitlement).split(".")[0], "base64url").toString());
  assert.equal(trialClaims.tier, "trial");
  assert.equal(trialClaims.exp, Math.floor(Date.parse(String(trial.ends_at)) / 1000));
  assert.ok(Math.abs(Date.parse(trial.ends_at) - Date.now() - 72 * 60 * 60 * 1000) < 5000);
  trial = (await h.store.list("trials", { account_id: xAccount.id }))[0];
  await h.store.atomic([{ kind: "update", table: "trials", where: { id: trial.id }, row: { ends_at: new Date(Date.now() - 1000).toISOString() } }]);
  const repost = await h.request("/v1/admin/reposts", {
    admin: true,
    data: { provider: "x", post_id: "123456789", reposters: [{ id: "42", handle: "fanx" }], source: "x_plugin" },
  });
  assert.equal(repost.status, 200);
  trial = (await h.store.list("trials", { id: trial.id }))[0];
  assert.ok(trial.converted_at);
});

test("X repost converts an active trial", async (t) => {
  const h = await harness(t, undefined, { GATE_X_LAUNCH_POST_ID: "123456789" });
  const created = await h.claim("x", "activefan");
  const account = (await h.store.list("accounts", { provider: "x", handle: "activefan" }))[0];
  await h.request("/v1/admin/followers", {
    admin: true,
    data: { provider: "x", followers: [{ id: "43", handle: "activefan" }], source: "x_plugin", complete: true },
  });
  const trial = (await h.store.list("trials", { account_id: account.id }))[0];
  assert.ok(Date.parse(trial.ends_at) > Date.now());
  const result = await h.request("/v1/admin/reposts", {
    admin: true,
    data: { provider: "x", post_id: "123456789", reposters: [{ id: "43", handle: "activefan" }], source: "x_plugin" },
  });
  assert.equal(result.status, 200);
  assert.ok((await h.store.list("trials", { id: trial.id }))[0].converted_at);
  assert.equal(created.status, 201);
});

test("repost before follow unlocks permanently when the follow later confirms", async (t) => {
  const h = await harness(t, undefined, { GATE_X_LAUNCH_POST_ID: "123456789" });
  const created = await h.claim("x", "repostfirst");
  const reposters = await h.request("/v1/admin/reposts", {
    admin: true,
    data: { provider: "x", post_id: "123456789", reposters: [{ id: "45", handle: "repostfirst" }], source: "x_plugin" },
  });
  assert.equal(reposters.status, 200);
  assert.equal((await h.store.list("trials")).length, 0);
  const followers = await h.request("/v1/admin/followers", {
    admin: true,
    data: { provider: "x", followers: [{ id: "45", handle: "repostfirst" }], source: "x_plugin", complete: true },
  });
  assert.equal(followers.status, 200);
  const polled = await h.poll(String(created.data.claim_id), created.device);
  assert.equal(polled.data.status, "unlocked");
  assert.ok(polled.data.entitlement);
  assert.equal((await h.store.list("trials")).length, 0);
});

test("LinkedIn trial converts on Comet-confirmed repost and rejects other sources", async (t) => {
  const h = await harness(t, undefined, {
    GATE_LINKEDIN_LAUNCH_POST_URL: "https://www.linkedin.com/posts/owner_launch-activity-1234567890",
  });
  const created = await h.claim("linkedin", "linkedfan");
  assert.equal(created.status, 201);
  assert.ok(created.data.trial_ends_at);
  const claimId = String(created.data.claim_id);
  const admin = await h.request(`/v1/admin/claims/${claimId}/verify`, {
    admin: true,
    data: { repost_confirmed: true, source: "x_plugin" },
  });
  assert.equal(admin.status, 400);
  const valid = await h.request(`/v1/admin/claims/${claimId}/verify`, {
    admin: true,
    data: { repost_confirmed: true, source: "comet" },
  });
  assert.equal(valid.status, 200);
  const trial = (await h.store.list("trials", { claim_id: claimId }))[0];
  assert.ok(trial.converted_at);
});

test("configured launch trial refresh expires with 410 and does not extend the launch deadline", async (t) => {
  const store = new MemoryStore();
  const firstSeen = new Date(Date.now() - 100 * 3600 * 1000).toISOString();
  await store.atomic([{ kind: "insert", table: "gate_settings", row: {
    key: "linkedin_launch_first_seen_at", value: firstSeen, updated_at: firstSeen,
  } }]);
  const h = await harness(t, store, {
    GATE_LINKEDIN_LAUNCH_POST_URL: "https://www.linkedin.com/posts/owner_launch-activity-1234567890",
  });
  const created = await h.claim("linkedin", "expirefan");
  const account = (await h.store.list("accounts", { provider: "linkedin", handle: "expirefan" }))[0];
  let trial = (await h.store.list("trials", { account_id: account.id }))[0];
  const end = trial.ends_at;
  const signed = signRequest(created.device, "refresh", { device_id: created.device.id });
  const fresh = await fetch(`${h.base}/v1/entitlement/refresh`, { method: "POST", headers: signed.headers, body: signed.body });
  assert.equal(fresh.status, 200);
  const refreshed = await fresh.json();
  const claims = JSON.parse(Buffer.from(refreshed.entitlement.split(".")[0], "base64url").toString());
  assert.equal(claims.tier, "trial");
  assert.equal(claims.exp, Math.floor(Date.parse(String(end)) / 1000));
  const after = (await h.store.list("trials", { id: trial.id }))[0];
  assert.equal(after.ends_at, end);
  await h.store.atomic([{ kind: "update", table: "trials", where: { id: trial.id }, row: { ends_at: new Date(Date.now() - 1000).toISOString() } }]);
  const expiredSigned = signRequest(created.device, "refresh", { device_id: created.device.id });
  const expired = await fetch(`${h.base}/v1/entitlement/refresh`, { method: "POST", headers: expiredSigned.headers, body: expiredSigned.body });
  assert.equal(expired.status, 410);
  assert.equal((await expired.json()).error, "trial_expired");
});

test("one trial per account and device", async (t) => {
  const h = await harness(t);
  const first = await h.claim("linkedin", "singletrial");
  assert.equal(first.status, 201);
  const duplicateAccount = await h.claim("linkedin", "singletrial", device());
  assert.ok([200, 201].includes(duplicateAccount.status));
  assert.notEqual(duplicateAccount.data.status, "trial");
  assert.equal(duplicateAccount.data.entitlement, null);
  const duplicateDevice = await h.claim("linkedin", "anothertrial", first.device);
  assert.ok([200, 201].includes(duplicateDevice.status));
  assert.notEqual(duplicateDevice.data.status, "trial");
  assert.equal(duplicateDevice.data.entitlement, null);
  assert.equal((await h.store.list("trials")).length, 1);
  const pending = await h.request("/v1/admin/claims?needs=repost&provider=linkedin", { admin: true });
  assert.equal(pending.status, 200);
});

test("existing X post and follow unlock path still unlocks", async (t) => {
  const h = await harness(t, undefined, { GATE_X_LAUNCH_POST_ID: "987654321" });
  const handle = "alreadyunlocks";
  const statusId = ((BigInt(Date.now() - 1288834974657) << 22n) + 1n).toString();
  const postUrl = xPostUrl(handle, statusId);
  seedGoodPost(h.mock.state, postUrl, handle, goodPostText());
  await h.store.atomic([
    { kind: "insert", table: "followers", row: { provider: "x", handle, x_user_id: "42" } },
  ]);
  const result = await h.claim("x", handle, device(), postUrl);
  assert.equal(result.status, 201);
  assert.equal(result.data.status, "unlocked");
  assert.ok(result.data.entitlement);
  const repeated = await h.claim("x", handle, result.device);
  assert.equal(repeated.data.status, "unlocked");
  assert.ok(repeated.data.entitlement);
  assert.equal((await h.store.list("trials")).length, 0);
});

test("legacy-domain post text verifies through the oEmbed mock", async (t) => {
  const h = await harness(t, undefined, { GATE_X_LAUNCH_POST_ID: "987654321" });
  const handle = "legacyfan";
  const postUrl = xPostUrl(handle, ((BigInt(Date.now() - 1288834974657) << 22n) + 1n).toString());
  seedGoodPost(h.mock.state, postUrl, handle, "Try https://ingpoc-agent-plugins.nicewasp128.chatgpt.site/?ref=acu-x");
  await h.store.atomic([{ kind: "insert", table: "followers", row: { provider: "x", handle, x_user_id: "44" } }]);
  const created = await h.claim("x", handle, device(), postUrl);
  assert.equal(created.data.status, "unlocked");
  assert.equal((await h.store.list("trials")).length, 0);
});

test("both providers accept a signed claim body with post_url absent", async (t) => {
  const h = await harness(t);
  for (const provider of ["x", "linkedin"]) {
    const d = device();
    const signed = signRequest(d, "claim", { provider, handle: "nourlfan", device_pubkey: d.key, device_name: "Test Mac" });
    const result = await h.request("/v1/claims", { method: "POST", body: signed.body, headers: signed.headers });
    assert.equal(result.status, 201);
    assert.equal(result.data.status, provider === "x" ? "pending_verification" : "trial");
  }
});

test("active trials can update gated wall preferences and preserve them on conversion", async (t) => {
  const h = await harness(t, undefined, { GATE_LINKEDIN_LAUNCH_POST_URL: "https://www.linkedin.com/posts/owner_launch" });
  const created = await h.claim("linkedin", "walltrial");
  const signed = signRequest(created.device, "wall", { device_id: created.device.id, show: true });
  const result = await h.request("/v1/wall", { method: "POST", body: signed.body, headers: signed.headers });
  assert.equal(result.status, 200);
  assert.equal(result.data.show, true);
  const converted = await h.request(`/v1/admin/claims/${created.data.claim_id}/verify`, { admin: true, data: { repost_confirmed: true, source: "comet" } });
  assert.equal(converted.data.status, "unlocked");
  const account = (await h.store.list("accounts", { provider: "linkedin", handle: "walltrial" }))[0];
  assert.equal(account.wall_opt_in, true);
});
