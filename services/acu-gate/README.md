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

### Recommended routine order

1. **`GET /v1/admin/claims`** — pending work list (`status=pending_verification`, optional `provider`, `limit`).
2. **`POST /v1/admin/followers`** — push Guru’s X follower snapshot; confirms follow on matching claims and may unlock when the post is already confirmed.
3. **`POST /v1/admin/claims/verify-batch`** — batch post verification (Comet/oEmbed evidence) for remaining X/LinkedIn claims.
4. **`POST /v1/admin/claims/{id}/reject`** — reject stale or bad claims (e.g. after 72h or spam).
5. **`GET /v1/admin/stats`** — optional dashboard read after a pass.

Use **`POST /v1/admin/accounts/{id}/revoke`** or **`POST /v1/admin/accounts/{id}/reset-rebind`** only for manual ops, not each cycle.

### `GET /v1/admin/claims`

Query: `status` (default `pending_verification`), optional `provider=x|linkedin`, `limit` (max 500, default 200).

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
  "post_text": "…#AgentComputerUse…",
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

**CLI:** `node scripts/gate-admin.mjs verify <claim_id> --post-url … --post-text … --follow --source comet --verified --followers-count 50`

### `POST /v1/admin/claims/verify-batch`

**Request:**

```json
{
  "results": [
    {
      "claim_id": "746e6d59-db80-4537-835d-c0a5443fe145",
      "post_url": "https://www.linkedin.com/posts/neon",
      "post_text": "…?ref=acu-li…#AgentComputerUse",
      "source": "comet"
    }
  ]
}
```

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

Handle entry is not cryptographic identity proof. Mitigations: fresh posts, one-use URLs, one Mac per account, rebind with cooldown. Follow checks use the pushed follower snapshot only at verification time. LinkedIn often needs Comet. Routine cadence drives latency; claims expire after 72 hours.
