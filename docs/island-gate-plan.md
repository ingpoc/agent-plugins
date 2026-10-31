# Dynamic Island gate (free, claim-and-verify): plan

Status: draft PR #52, not merged, not deployed. Deploy and release need Guru's approval.
Final scope (Guru, 2026-10-31 20:04 and 20:07 IST): no X API credits or any paid API, no OAuth, users use any browser, verification runs on OUR side (Guru's Grok Bot / ACU routine with Guru's free X plugin and Guru's Comet).

## Product rules
- comet-control, macos-cua and Samantha stay free and unlocked. Only the Dynamic Island is gated. The island is free for everyone, no cap.
- Unlock = follow @gurusharan on X + post Guru's preset (route X, primary). No X account: LinkedIn route (follow nudge, share the preset, our side reads the post; no follow check).
- One person (provider + handle) = one unlock = one Mac. 14-day offline grace.
- Locked: island panel hidden, Samantha shows her state in the menu bar, nothing else changes.
- Public: total unlocks, downloads (GitHub DMG download_count), opt-in "who's using it" wall. Private: admin stats. Ref click-throughs (`?ref=acu-x`, `?ref=acu-li`).

## Docs checked (2026-10-31)
| Topic | Source | What we rely on |
|---|---|---|
| X oEmbed | https://docs.x.com/x-for-websites/oembed-api | `GET https://publish.x.com/oembed?url=<post url>`: "Requires authentication? No", "Rate limited: No", JSON with `author_name`, `author_url`, `html` (post text). Tested 2026-10-31: publish.twitter.com 301 → publish.x.com, 200, no key; unknown post → 404. Free, no developer app. |
| X web intents | https://docs.x.com/x-for-websites/web-intents/overview | `https://x.com/intent/post?text=` (prefilled post; the user presses Post), follow via the profile page `https://x.com/gurusharan` |
| X post id time | X ids are Snowflake ids: `(id >> 22) + 1288834974657` = creation ms | freshness check of a claimed post without any API |
| X API pricing (why no API) | https://docs.x.com/x-api/getting-started/pricing | pay-per-use only, no free tier → not used anywhere |
| LinkedIn share | https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/plugins/share-plugin | only URL sharing (`share-offsite/?url=`) is documented; prefilled text is not, so the app copies the text to the clipboard |
| ASWebAuthenticationSession | (no longer used) | removed with OAuth |
| Keychain | https://developer.apple.com/documentation/security/ksecattraccessibleafterfirstunlockthisdeviceonly | device key, claim id and entitlement stored ThisDeviceOnly |
| Render web services | https://render.com/docs/web-services , https://render.com/docs/blueprint-spec | bind 0.0.0.0:$PORT, healthCheckPath, secrets `sync: false` |
| Neon serverless driver | https://neon.com/docs/serverless/serverless-driver | `neon()` HTTP queries, non-interactive transactions |

## User flow (ACU app, any default browser, no scraping on the user's Mac)
1. Locked sheet "Unlock the island": step 1 "Follow @gurusharan" opens `https://x.com/gurusharan` with `NSWorkspace.open` (default browser). Step 2 "Post about ACU" opens `https://x.com/intent/post?text=<preset>`; the user presses Post themselves. Step 3: type `@handle` once (post link optional) → "Check".
2. App `POST /v1/claims` (device-signed). Server creates claim `pending_verification` bound to this Mac. If a post URL was given, the server checks it immediately through oEmbed.
3. App shows "Checking… usually a few minutes" and polls `GET /v1/claims/{id}` (60 s, backing off to 5 min; also at launch). On `unlocked` it stores the entitlement and the island appears. No further action from the user.
4. LinkedIn ("No X account?"): "Follow Gurusharan on LinkedIn" (profile URL from config, not checked), "Share on LinkedIn" (`share-offsite/?url=<ref link>` + preset copied to clipboard), then the user enters their LinkedIn profile URL (handle = `/in/<slug>`) and optionally the post URL → same claim + poll. The server tries one public fetch of a given post URL; otherwise our side reads the post with Comet.

## Our side (routine owned by Grok Bot; this PR builds endpoints + `services/acu-gate/scripts/gate-admin.mjs`)
1. `GET /v1/admin/claims?status=pending_verification` → work list.
2. Follows: read Guru's follower list with Guru's connected X plugin (free) and push it: `POST /v1/admin/followers`. The server matches pending X claims by handle (or id) and marks `follow_confirmed`.
3. Posts: for X claims without a confirmed post, our side finds the post (oEmbed if a URL is known, else Guru's Comet search `#AgentComputerUse from:<handle>`), then `POST /v1/admin/claims/{id}/verify` with the post URL, text and source. For X post URLs the server re-checks through oEmbed itself; `source: "comet"` evidence is accepted only when oEmbed cannot see the post (protected/deleted), and is recorded as such.
4. LinkedIn: Comet on our side reads the user's post/activity, then `verify` with `source: "comet"`.
5. A claim with follow (X only) + post confirmed flips to `unlocked`: unlock row, Mac binding, wall consent applied. The app picks it up on its next poll.

## Match rules
- Preset (server config only, `services/acu-gate/config/preset.json`): X "Just unlocked the Dynamic Island in Agent Computer Use. My agents' runs now show live at the top of my Mac. Free, by @gurusharan https://ingpoc-agent-plugins.nicewasp128.chatgpt.site/?ref=acu-x #AgentComputerUse"; LinkedIn same with "Gurusharan Gupta" and `?ref=acu-li`.
- A post matches when its text contains `#AgentComputerUse` (case-insensitive) or the ref link.
- X post must be authored by the claimed handle (oEmbed `author_url`), created no earlier than 24 h before the claim (Snowflake time), and each post URL is usable once.

## One person, one unlock, one Mac
- Account key = (provider, normalized handle): X handle lowercased without `@`; LinkedIn `/in/<slug>` lowercased. Our side may attach the X numeric id from the follower list; handle renames are matched by id when present.
- Unique: `accounts(provider, handle)`, `unlocks.account_id`, `bindings.account_id`, `bindings.device_id`, `used_posts(post_key)`.
- A Mac already bound to another account → `device_taken`. An account already bound to another Mac → the claim needs a NEW post (fresh, unused URL, created after the existing binding) and the rebind cooldown (7 days); the old Mac's refresh gets `device_moved`.
- Cross-provider: X and LinkedIn are separate accounts, but each Mac holds one account, so a person gets at most one island per Mac. No email matching (we collect no email).

## Honest limits (accepted tradeoffs)
- Without OAuth the user is not authenticated. Someone could type another person's handle whose fresh qualifying post exists; mitigations: post freshness (24 h before claim), one-use post URLs, one Mac per account, and the real owner can move the island with a new post. Low harm: the island is free.
- The follow check is our side's read of Guru's follower list at verification time; later unfollows are not re-checked (optional future routine).
- LinkedIn has no follow check and public post pages are often login-walled, so most LinkedIn claims wait for our side.
- Verification latency = routine cadence: Guru's routine runs hourly, so a claim usually unlocks within the hour (instantly when the follower list already has the handle and the post URL passes oEmbed). The wait text comes from `/v1/config` (`check_eta_text`). Claims expire after 72 h without verification (user can resubmit).
- Render free plan (Guru, 20:10 IST): the service sleeps after idle and cold-starts in ~30-60 s. The app treats timeouts/502/503/504 during the first 90 s as "Waking up the unlock service…", never as an error; polls stay silent on network errors. The 14-day grace makes sleep invisible to unlocked users.
- First run on a clean Mac needs nothing but ACU and a default browser: no Comet, no X/LinkedIn developer app, no sign-in inside ACU.

## Offline grace: 14 days
Entitlement Ed25519 `exp = iat + 14 d`, refreshed every 24 h (`POST /v1/entitlement/refresh`, device-signed). Covers travel and server sleep, bounds how long a moved/revoked Mac keeps the island; `iat` more than 1 day in the future is rejected.

## API contract
JSON. Errors `{"error":"<code>","message":"..."}`. Codes: `invalid_handle`, `post_url_invalid`, `post_url_used`, `post_not_found`, `post_author_mismatch`, `post_too_old`, `post_missing_tag`, `follow_missing`, `claim_expired`, `rejected`, `device_taken`, `device_conflict` (extra `rebind_available_at`), `rebind_cooldown`, `device_moved`, `revoked`, `bad_signature`, `rate_limited` (`retry_after`), `not_found`, `server_error`.
Device signature: base64 DER ECDSA P-256 SHA-256 over `acu-gate|<action>|<device_id>|<ts>|<body_sha256_hex>`; `ts` unix seconds ±300 s; replay window 10 min. Device key = raw X9.63 P-256 public key, base64. `device_id` = hex SHA-256 of "acu-island-gate|" + IOPlatformUUID.

Public / app:
- `GET /healthz`, `GET /v1/config` → `{"x":{"handle":"gurusharan","profile_url":"https://x.com/gurusharan","intent_url":"...","preset_text":"..."},"linkedin":{"follow_url":"...","share_url":"...","preset_text":"..."},"match":{...},"grace_days":14,"poll_seconds":60,"check_eta_text":"usually within the hour"}`
- `POST /v1/claims` headers `X-ACU-Device`, `X-ACU-TS`, `X-ACU-Sig` (action `claim`); body `{"provider":"x|linkedin","handle":"...","post_url":null|"...","device_pubkey":"...","device_name":"...","show_on_wall":false}` → 201 `{"claim_id","status":"pending_verification","checks":{"follow":"pending|confirmed|n/a","post":"pending|confirmed|failed"},"poll_after":60}` (or 200 `unlocked` + `entitlement` when everything is already confirmed). Same device + same account reuses the open claim.
- `GET /v1/claims/{id}` (signed, action `poll`) → `{"status":"pending_verification|unlocked|rejected|expired","checks":{...},"reason":null|"<code>","entitlement":null|"..."}`
- `POST /v1/entitlement/refresh` (signed, action `refresh`) → `{"entitlement"}` | 409 `device_moved` | 403 `revoked`
- `POST /v1/wall {show}` (signed, action `wall`) → `{"show"}`
- `GET /v1/public/counter` → `{"unlocks","updated_at"}`; `GET /v1/public/site-stats` → `{"downloads":{"github_dmg","redirect_hits","total"},"unlocks","wall":[{"handle","provider","avatar_url","profile_url","unlocked_at"}],"updated_at"}`; `GET|POST /v1/public/ref-click?ref=acu-x|acu-li` → 204; `GET /v1/public/download` → 302 latest DMG.
Entitlement: `base64url(payload).base64url(ed25519 sig)`, payload `{"v":1,"acct","prov","handle","device_id","iat","exp"}`; app verifies with the compiled-in public key (placeholder fails closed until release).

Admin (Bearer `ADMIN_TOKEN`, constant-time compare, failed-auth backoff) — for the routine:
- `GET /v1/admin/claims?status=pending_verification&provider=x|linkedin&limit=200` → `{"claims":[{"id","provider","handle","x_user_id","post_url","checks":{"follow","post"},"created_at","expires_at","device_name"}]}`
- `POST /v1/admin/followers` `{"provider":"x","followers":[{"id":"123","handle":"alice"}],"source":"x_plugin","complete":false}` → `{"upserted":n,"follow_confirmed_claims":[ids],"unlocked_claims":[ids]}`. Idempotent; `complete:true` marks the snapshot as the full list (enables unfollow stats only).
- `POST /v1/admin/claims/{id}/verify` `{"follow_confirmed":true|false|null,"post_url":"...","post_text":"...","author_handle":"...","source":"oembed|comet|x_plugin|manual","verified":bool|null,"verified_type":null|"blue",...,"followers_count":n|null,"avatar_url":null|"https://pbs.twimg.com/..."}` → claim. Server re-checks X posts via oEmbed; mismatches return 422 with the code and leave the claim pending.
- `POST /v1/admin/claims/verify-batch` `{"results":[ ...same objects with "claim_id"... ]}` → per-item results.
- `POST /v1/admin/claims/{id}/reject` `{"reason":"follow_missing|post_missing|post_author_mismatch|spam|other"}`
- `GET /v1/admin/stats` → `{"unlocks":{"x","linkedin","total"},"verified_unlocks","verified_by_type","reach","claims":{"pending","rejected","expired"},"followers_known","ref_clicks":{...},"downloads":{...},"wall_opt_in"}`
- `POST /v1/admin/accounts/{id}/revoke`, `POST /v1/admin/accounts/{id}/reset-rebind`

## Where our side keeps its settings (Mini)
- Public base URL: `~/.config/acu-gate/gate.json` → `{"base_url":"https://<render-host>"}` (not secret).
- Admin token: macOS Keychain generic password, service `acu-gate`, account `admin_token` (read inside `gate-admin.mjs`; never printed). Alternative for non-macOS: a 0600 file named by `ACU_GATE_ADMIN_TOKEN_FILE`.
