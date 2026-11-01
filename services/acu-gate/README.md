# ACU Gate

Claim-and-verify API for the Dynamic Island unlock flow. No OAuth and no paid X API usage: the app submits a handle (and optional post URL); verification uses oEmbed, a follower snapshot from the admin routine, and optional Comet evidence.

## Run locally

```bash
npm ci
npm run dev:local
```

Uses in-memory storage unless `ACU_GATE_DATABASE_URL_FILE` points at a Neon URL file. Writes the dev Ed25519 public key to `~/.config/acu-gate/dev-public-key.txt`. Port: `ACU_GATE_DEV_PORT` (default `8790`). Mock oEmbed and GitHub live on the same mock HTTP server.

## Tests

```bash
npm test
ACU_GATE_DATABASE_URL_FILE=$HOME/.config/acu-gate/neon-dev.url npm run test:neon
```

## Environment

See `.env.example`. Production on Render uses the **free** web plan: the service sleeps when idle and cold starts typically take **30–60 seconds**. The app’s 14-day entitlement grace and claim polling tolerate that; use `gate-admin.mjs --wake` for the verification routine.

Secrets on Render: `PUBLIC_BASE_URL`, `DATABASE_URL`, `ENTITLEMENT_PRIVATE_KEY`, `ADMIN_TOKEN`, optional `GITHUB_TOKEN`, `TRUST_PROXY_HOPS=1`.

Launch-trial configuration: set `GATE_X_LAUNCH_POST_ID` to the X launch post ID and `GATE_LINKEDIN_LAUNCH_POST_URL` to the LinkedIn launch post URL. Both are unset in the shipped presets. These identify the launch posts used by the trial claim flow.

## Mini: base URL and admin token

| What | Where on the Mac mini |
| --- | --- |
| Gate base URL | `~/.config/acu-gate/gate.json` → `{"base_url":"https://…"}` (used by `gate-admin.mjs` when `--base` and `ACU_GATE_BASE_URL` are unset) |
| Admin bearer token | macOS Keychain: service **`acu-gate`**, account **`admin_token`**; or set `ACU_GATE_ADMIN_TOKEN_FILE` to a file containing the token (never commit it) |

## Admin API for the verification routine

All admin routes require:

```http
Authorization: Bearer <ADMIN_TOKEN>
```

Failed auth returns `401` with `{"error":"not_found",…}` (intentionally vague). More than ten failed attempts per minute per IP returns `429` with `{"error":"rate_limited","retry_after":…}`.

Validation failures on verify often return `422` with codes such as `post_missing_tag`, `post_author_mismatch`, `post_not_found`, `post_too_old`. Missing rows return `404` with `{"error":"not_found",…}`.

### Recommended launch-trial workflow

The app accepts a handle for both X and LinkedIn. For LinkedIn, the handle may be a vanity name or profile URL; users do not paste a launch-post link. X trials start when the follow is confirmed. LinkedIn trials start when the claim is created. Both trial paths allow access to become permanent after the required post and follow/repost checks are confirmed; when all required evidence is already confirmed, the claim unlocks directly without creating a trial. A repost received before the X follow is retained, and the claim unlocks directly when the follow later confirms.

Before a provider's launch post is configured, its trial entitlement rolls forward on poll and refresh, including after the previous trial end time. While the launch is still unconfigured, an unconverted trial can also continue polling after its original 30-day claim expiry; each prelaunch check renews the claim's eligibility for another 30 days. This keeps prelaunch users active while the launch post is unavailable.

Once a launch post is configured, the service persists that provider's first-seen time in `gate_settings` as `<provider>_launch_first_seen_at`. This history is immutable: removing the launch setting later does not restore rolling prelaunch access. Unconverted trial access then ends at the later of the existing trial end and 72 hours after that persisted first-seen time. The first-seen deadline survives restarts and is tracked separately for X and LinkedIn, so restarting the service does not restart the 72-hour period. An existing prelaunch claim stays eligible for at least 30 days after that same persisted first-seen time; a newer claim retains its own 30-day TTL. Converted trials remain permanent. Trials are limited to one per account and one per device across both providers.

1. **`GET /v1/admin/claims`** — inspect outstanding follow or repost work; filter with `needs=follow|repost` and `provider=x|linkedin`.
2. **`POST /v1/admin/followers`** — push @gurusharan’s X follower snapshot.
3. **`POST /v1/admin/reposts`** — push the X launch-post reposter snapshot for pending claims.
4. **`POST /v1/admin/claims/{id}/verify`** — record a confirmed repost when verifying an individual claim.
5. **`GET /v1/admin/stats`** — optional dashboard read after a pass.

The CLI equivalents are `node scripts/gate-admin.mjs pending [--needs follow|repost] [--provider x|linkedin]`, `push-followers <file.json>`, `push-reposters <file.json>`, and `verify <id> --repost [--source comet]`.

`POST /v1/admin/reposts` accepts `{ "provider": "x", "post_id": "…", "reposters": [{ "id": "…", "handle": "…" }], "source": "x_plugin" }`. The post ID must match `x.launch_post_id`. For LinkedIn, use individual claim verification with `repost_confirmed: true` and `source: "comet"`; X individual repost verification requires `source: "x_plugin"`.

### Existing claim verification routine

1. **`GET /v1/admin/claims`** — pending work list (`status=pending_verification`, optional `provider`, `limit`).
2. **`POST /v1/admin/followers`** — push @gurusharan’s X follower snapshot; confirms follow on matching claims.
3. **`POST /v1/admin/claims/verify-batch`** — batch post verification (Comet/oEmbed evidence) for remaining claims.
4. **`POST /v1/admin/claims/{id}/reject`** — reject stale or bad claims.
5. **`GET /v1/admin/stats`** — optional dashboard read after a pass.

Use **`POST /v1/admin/accounts/{id}/revoke`** or **`POST /v1/admin/accounts/{id}/reset-rebind`** only for manual ops, not each cycle.

### `GET /v1/admin/claims`

Query: `status` (default `pending_verification`), optional `needs=follow|repost`, `provider=x|linkedin`, `limit` (max 500, default 200).

`post_ok: true` substitutes for matching post text when a valid post URL is supplied (X requires `source: "comet"`); `false` rejects with `post_not_ok`, and omission retains text matching.

**Response (example from tests):**

```json
{
  "claims": [
    {
      "id": "746e6d59-db80-4537-835d-c0a5443fe145",
      "provider": "x",
      "handle": "fan1",
      "x_user_id": null,
      "post_url": "https://x.com/fan1/status/1999999999999999999",
      "checks": { "follow": "pending", "post": "confirmed" },
      "created_at": "2026-10-31T12:00:00.000Z",
      "expires_at": "2026-11-03T12:00:00.000Z",
      "device_name": "Test Mac"
    }
  ]
}
```

**CLI:** `node scripts/gate-admin.mjs --wake list` · `node scripts/gate-admin.mjs list --provider x`

### `POST /v1/admin/followers`

**Request:**

```json
{
  "provider": "x",
  "followers": [{ "id": "9", "handle": "fan1" }],
  "source": "x_plugin",
  "complete": false
}
```

**Response (example from tests):**

```json
{
  "upserted": 1,
  "follow_confirmed_claims": ["746e6d59-db80-4537-835d-c0a5443fe145"],
  "unlocked_claims": ["746e6d59-db80-4537-835d-c0a5443fe145"],
  "results": [{ "claim_id": "746e6d59-db80-4537-835d-c0a5443fe145", "status": "unlocked", "checks": { "follow": "confirmed", "post": "confirmed" }, "reason": null }]
}
```

The response also includes `results`, with each processed claim's `claim_id`, `status`, `checks`, and `reason`. A blocked verified claim is `rejected` with reason `rebind_cooldown`, `post_url_used`, `post_too_old`, or `device_taken`. Cooldown responses include the ISO `rebind_available_at`; the user can submit a new claim after that time.

**CLI:** `node scripts/gate-admin.mjs --wake push-followers followers.json`

### `POST /v1/admin/claims/{id}/verify`

**Request (X, Comet when oEmbed 404):**

```json
{
  "follow_confirmed": true,
  "post_url": "https://x.com/comet1/status/1999999999999999999",
  "post_ok": true,
  "source": "comet",
  "verified": true,
  "verified_type": "blue",
  "followers_count": 50,
  "avatar_url": "https://pbs.twimg.com/profile_images/example.jpg"
}
```

**Response (example from tests):**

```json
{
  "claim_id": "746e6d59-db80-4537-835d-c0a5443fe145",
  "status": "unlocked",
  "checks": { "follow": "confirmed", "post": "confirmed" }
}
```

Verify and verify-batch return the same status, checks, reason, and optional cooldown date for each claim.

**CLI:** `node scripts/gate-admin.mjs verify <claim_id> --post-url … --post-ok --follow --source comet --verified --followers-count 50`

### `POST /v1/admin/claims/verify-batch`

**Request:**

```json
{
  "results": [
    {
      "claim_id": "746e6d59-db80-4537-835d-c0a5443fe145",
      "post_url": "https://www.linkedin.com/posts/neon",
      "post_ok": true,
      "source": "comet"
    }
  ]
}
```

Each item accepts `post_ok`: `true` substitutes for matching text with a valid post URL (X requires `source: "comet"`); `false` rejects that item, and omission retains existing behavior.

**Response:**

```json
{
  "results": [
    {
      "claim_id": "746e6d59-db80-4537-835d-c0a5443fe145",
      "status": "unlocked",
      "checks": { "follow": "n/a", "post": "confirmed" }
    }
  ]
}
```

**CLI:** `node scripts/gate-admin.mjs verify-batch batch.json`

### `POST /v1/admin/claims/{id}/reject`

**Request:**

```json
{ "reason": "spam" }
```

**Response (example from tests):**

```json
{ "status": "rejected", "reason": "spam" }
```

Allowed reasons include `follow_missing`, `post_missing`, `post_author_mismatch`, `spam`, `other`.

**CLI:** `node scripts/gate-admin.mjs reject <claim_id> spam`

### `GET /v1/admin/stats`

**Response (shape from tests; numbers vary):**

```json
{
  "unlocks": { "x": 1, "linkedin": 0, "total": 1 },
  "verified_unlocks": 1,
  "verified_by_type": { "blue": 1 },
  "reach": 50,
  "claims": { "pending": 0, "rejected": 0, "expired": 0 },
  "followers_known": 1,
  "ref_clicks": { "acu-x": 1, "acu-li": 0 },
  "downloads": { "github_dmg": 42, "redirect_hits": 1, "total": 43 },
  "wall_opt_in": 0
}
```

**CLI:** `node scripts/gate-admin.mjs --wake stats`

### `POST /v1/admin/accounts/{id}/revoke`

**Request:** `{}` (empty JSON body).

**Response:**

```json
{ "ok": true }
```

Revoked accounts get `403 revoked` on refresh; wall opt-in cleared.

### `POST /v1/admin/accounts/{id}/reset-rebind`

**Request:** `{}`.

**Response:**

```json
{ "ok": true }
```

Clears `last_rebind_at` so the account can rebind after cooldown rules with a fresh post.

## Public HTTP surface

`GET /healthz`, `GET /v1/config` (includes `poll_seconds: 60` and `check_eta_text`), `POST /v1/claims`, `GET /v1/claims/{id}`, `POST /v1/entitlement/refresh`, `POST /v1/wall`, `GET /v1/public/counter`, `GET /v1/public/site-stats`, `GET|POST /v1/public/ref-click`, `GET /v1/public/download`.

## Honest limits

Handle entry is not cryptographic identity proof. A fixed public key and client-supplied device ID prevent simple identity reuse, but a modified client can generate fresh signing keys for new claims. The service does not attest hardware identity, so this does not prove one physical Mac per trial. Fresh posts, one-use URLs, one account per accepted device key, and rebind cooldown reduce abuse but do not eliminate it. Follow checks use the pushed follower snapshot only at verification time. LinkedIn often needs Comet. Routine cadence drives latency; ordinary claims expire after 30 days.

### Trial entitlements and compatibility

`POST /v1/claims` accepts `{provider, handle}` plus the existing signed device fields, without `post_url`. Legacy post URLs and preset posts using the previous site domain still verify. Claim IDs remain eligible for conversion for 30 days. Existing permanent unlock rows and bindings are preserved by the additive `002_trials.sql` migration.

Active claim creation and polls return `status: "trial"`, `trial_ends_at`, `checks`, `poll_after: 300`, and the normal signed entitlement with `tier: "trial"`. The token expires at the stored trial end. `POST /v1/entitlement/refresh` rolls that end forward while the provider has no launch post. Once configured, refresh respects the persisted launch window and returns HTTP 410 with `error: "trial_expired"` after it ends. Permanent unlock refresh is unchanged. A repost can still convert a pending claim after its trial ends.

`GET /v1/config` exposes the launch targets, derived repost URLs, `trial_hours: 72`, and `trial_eta_text`, while retaining the legacy post and share fields. `GET /v1/admin/claims` adds `trial_ends_at` to each row. A complete follower snapshot reports missing active trial handles in `trial_unfollowed`; it does not revoke their trial.

Active trial devices can also use the signed wall-preference route. Their choice is retained when the trial converts; public wall listings still require a permanent unlock.


### Security invariants

Migration `003_device_keys.sql` adds the trial public key and a unique index, backfills a durable signing-key/device-ID registry from existing claims and bindings, and retains every consumed trial row. Legacy duplicate-key trial records are preserved; only the earliest receives the indexed key. A claim signed by a key already registered to another device ID returns `409 device_conflict`. Generating fresh keys on a modified client remains outside this software-only identity guarantee.

Follower and reposter snapshots validate each ID/handle pair before applying it. Established X IDs are immutable, and durable unique identity pairs prevent simultaneous pushes from assigning one ID to different handles. Handle-only legacy follower entries cannot match an established account ID. Authenticated admin requests have a 120-request burst with refill at 120/minute per IP; snapshots cap entries at 5,000 (the existing raw-body byte cap also applies), and each follow/repost pass processes at most 500 claims still needing that check. Repeat pushes can process the remaining claims.

Rebinding compares the observed device ID, bound time and last rebind time inside the same atomic transaction that consumes the post and changes the binding. A losing transaction rolls back and re-evaluates current cooldown and post-use state before retrying.
