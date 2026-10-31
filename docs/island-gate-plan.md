# Dynamic Island gate (free, verify-to-unlock): plan

Status: draft PR, not merged, not deployed. Deploy, release and X API credit purchase need Guru's approval.

## Product rules (Guru, 2026-10-31)
- comet-control, macos-cua and Samantha stay free and unlocked. Only the Dynamic Island is gated.
- The island is free for everyone. Unlock = (X route) follows @gurusharan and posts Guru's preset text on X, or (LinkedIn route) posts the preset text on LinkedIn and pastes the post URL. No payment, no cap.
- One person = one unlock = one Mac. Rebinding goes through a safe path.
- Locked island: the island panel stays hidden; Samantha keeps working and shows her state in the menu bar status item. Nothing else changes.
- A public counter shows total unlocks (JSON endpoint + snippet for the Agent Plugins site; site deploy needs Guru).

## Docs checked (2026-10-31)
| Topic | Source | What we rely on |
|---|---|---|
| X OAuth 2.0 PKCE | https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code | authorize `https://x.com/i/oauth2/authorize`, token `https://api.x.com/2/oauth2/token`, S256 PKCE, exact-match callback, auth code expires in 30 s, access token 2 h unless `offline.access`; Web App = confidential client (has secret) |
| X users/me | https://docs.x.com/x-api/users/get-my-user | id, username, `verified`, `verified_type` via `user.fields` |
| X follows | https://docs.x.com/x-api/users/follows/introduction, /x-api/users/get-followers, /x-api/users/get-following | no "does A follow B" endpoint in v2; lists only, paginated |
| X user posts | https://docs.x.com/x-api/users/get-posts | `GET /2/users/:id/tweets`, `max_results` 5..100, `start_time`, `exclude` |
| X recent search | https://docs.x.com/x-api/posts/search/introduction | last 7 days, all developers |
| X pricing | https://docs.x.com/x-api/getting-started/pricing , /x-api/getting-started/free-credits | pay-per-use only, no free tier: User read $0.010, Following/Followers read $0.010 per resource, Posts read $0.005, Owned Reads $0.001 when the authenticated user owns the developer app (followers/following/tweets of the owner); daily dedup; up to $70 promo credits after a first purchase |
| LinkedIn OIDC | https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin-v2 | scopes `openid profile email`; `GET https://api.linkedin.com/v2/userinfo`; `sub` is pairwise (per app); email optional; "does not verify user identities"; 500 req/member/day |
| LinkedIn auth code | https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow | HTTPS absolute redirect URL registered on the Auth tab; token exchange needs `client_secret` (server-side) |
| LinkedIn share | https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/plugins/share-plugin | only the Share Plugin is documented (a URL share, `share-offsite/?url=`). Prefilled text (`/feed/?shareActive=true&text=`) is NOT documented. Posting via API needs `w_member_social` (Share on LinkedIn product), which we do not request |
| ASWebAuthenticationSession | https://developer.apple.com/documentation/authenticationservices/aswebauthenticationsession | macOS opens the default browser (or Safari), only the calling app's session gets the callback even if another app registers the scheme; `prefersEphemeralWebBrowserSession` |
| Keychain | https://developer.apple.com/documentation/security/storing-keys-in-the-keychain , .../ksecattraccessibleafterfirstunlockthisdeviceonly | generic-password items; `AfterFirstUnlockThisDeviceOnly` items never migrate to another Mac (fits a one-Mac binding, readable by a background agent after first unlock) |
| Render web services | https://render.com/docs/web-services , https://render.com/docs/blueprint-spec | bind `0.0.0.0:$PORT` (default 10000), `healthCheckPath`, secrets as `sync: false` env vars |
| Neon serverless driver | https://neon.com/docs/serverless/serverless-driver | `neon()` over HTTP for single queries and non-interactive transactions; WebSocket `Pool` only for interactive transactions |

## X API tier: blocker
There is no free X API tier any more. Every call the X route needs is paid per use:
- sign-in identity `GET /2/users/me`: $0.010 per sign-in (no way around it: the OAuth token has no user id);
- post check `GET /2/users/:id/tweets?max_results=5&start_time=now-7d`: $0.005 per post returned, at most ~$0.025-0.05;
- follow check: cheapest path is an Owned Read of @gurusharan's own followers (`GET /2/users/{guru_id}/followers`, $0.001 per follower) with Guru's own user token, synced incrementally (stop at the first already-known id). Checking the user's `following` list instead costs $0.010 per account returned and is a fallback only.
Expected cost about $0.02-0.07 per verified user. Guru must buy X API credits (payment = his approval) and the developer app must be owned by @gurusharan for the $0.001 owned-read rate. Until then the X route can only run against the mock. Fallback if Guru declines credits: LinkedIn route + manual approval queue only.

## Flow
1. App opens `ASWebAuthenticationSession` at `{GATE}/v1/auth/{x|linkedin}/start?device_id&device_name&app_challenge`, callback scheme `acu-island`.
2. Server keeps the provider PKCE verifier/state/nonce, redirects to the provider, gets the callback on `{GATE}/v1/auth/{p}/callback`, exchanges the code with the client secret, reads identity (X users/me; LinkedIn userinfo), then redirects to `acu-island://auth?code=<one-time, 60 s>`.
3. App `POST /v1/session/redeem {code, app_verifier, device_id, device_pubkey}` (app-side PKCE: S256(app_verifier) must equal app_challenge). Returns an opaque session (hashed in DB, 30 d) + account status + the user's share links. Secrets never reach the app.
4. Share: the preset texts live only in server config (`services/acu-gate/config/preset.json`, served by `GET /v1/config`; the app never hard-codes copy). Guru's text (2026-10-31):
   - X: `Just unlocked the Dynamic Island in Agent Computer Use. My agents' runs now show live at the top of my Mac. Free, by @gurusharan https://ingpoc-agent-plugins.nicewasp128.chatgpt.site/?ref=acu-x #AgentComputerUse`
   - LinkedIn: same text with `Gurusharan Gupta` instead of `@gurusharan` and `?ref=acu-li`.
   - Match rule: a post matches when it contains `#AgentComputerUse` (case-insensitive) or the ref link (`?ref=acu-x` / `?ref=acu-li`, also via X `entities.urls[].expanded_url`, since X wraps links in t.co).
   X opens `https://x.com/intent/post?text=<urlencoded>`. LinkedIn opens the documented `https://www.linkedin.com/sharing/share-offsite/?url=<urlencoded ref link>` and copies the full preset to the clipboard ("Paste with Cmd-V"); the undocumented `feed/?shareActive=true&text=` is behind config `linkedin_prefill_text` (off by default).
5. Verify:
   - X `POST /v1/verify/x`: follow (owner-followers cache, incremental sync; fallback user following) AND a post in the last 7 days that matches the rule above (the user's own timeline, so the author is the signed-in user). Missing parts come back separately so the UI can say exactly what is left.
   - LinkedIn `POST /v1/verify/linkedin {post_url}`: URL must be `https://www.linkedin.com/(posts|feed/update)/...`; each post URL usable once. Server fetches the public page (8 s timeout, no cookies). Auto-pass only if the page matches the rule AND its author name equals the LinkedIn OIDC `name` (the only author signal available: `sub` is pairwise and never appears on the page). If LinkedIn serves an auth wall/999/non-200 (common), the claim goes to the `pending` queue for manual approval (admin endpoints). The app shows "Waiting for review" and polls.
6. Unlock: on pass the server binds the account to this Mac and returns an Ed25519-signed entitlement `{v, acct, prov, handle, device_id, iat, exp}`; the app checks it offline with the public key compiled into the app.

## One person, one unlock, one Mac (dedup)
- `accounts` = person; `identities(provider, provider_user_id)` unique, points to one account. `unlocks.account_id` unique, `bindings.account_id` unique and `bindings.device_id` unique.
- `device_id` = SHA-256 of the Mac's IOPlatformUUID + app salt (stable across reinstalls) and the device holds a P-256 key in the Keychain (ThisDeviceOnly) that signs refresh and rebind.
- Second provider on the same Mac: if a session exists the new identity is LINKED to the current account (explicit, signed-in link). Without a session the Mac is already bound to another account → `device_taken`, offer "Sign in with the account that unlocked this Mac".
- No automatic email merge: LinkedIn says its OIDC does not verify identity and email is optional; X email needs an extra scope and is optional; merging on email enables account takeover. Emails are stored only as an HMAC (`EMAIL_HASH_KEY`) and shown to the reviewer on LinkedIn pending claims when they collide with an unlocked account.
- Residual: a LinkedIn author-name match is weak (names are not unique), so any doubt goes to the pending queue. One person with two Macs, an X account and a LinkedIn account could unlock twice. Accepted: the island is free, each unlock still costs a public post, and the Mac binding caps it at one per Mac.

## Rebind (one Mac)
Account bound elsewhere → app shows "The island is active on <name> since <date>. Move it to this Mac?". `POST /v1/device/rebind` needs a fresh sign-in session from this Mac (proves account ownership) and is limited to once per 7 days (after that the reviewer can reset). The old Mac's next refresh gets `device_moved` and locks.

## Offline grace: 14 days
Entitlement `exp = iat + 14 d`, refreshed every 24 h when online (silent). 14 days covers travel and a dead Render free-tier instance without bothering anyone, and bounds how long a moved/revoked Mac keeps the island. Clock rollback is ignored except that a token with `iat` more than 1 day in the future is rejected.

## Counter
`GET /v1/public/counter` → `{"unlocks": n, "updated_at": ...}`, CORS `*`, `Cache-Control: public, max-age=60`. Snippet: `services/acu-gate/site/counter-snippet.html`.

## Optional later
LinkedIn API posting (`w_member_social`) to skip the paste step; X Activity API `follow.follow` webhook ($0.010/event) instead of follower sync.

## API contract (server `services/acu-gate`, app `IslandGate*.swift`)
All JSON, UTF-8. Errors: HTTP 4xx/5xx with `{"error": "<code>", "message": "<human>", ...extra}`. Error codes the app must handle: `not_signed_in`, `session_expired`, `follow_missing`, `post_missing`, `post_url_invalid`, `post_url_used`, `pending_review`, `rejected`, `device_conflict` (extra `device_name`, `bound_at`, `rebind_available_at`), `device_taken`, `rebind_cooldown` (extra `rebind_available_at`), `device_moved`, `revoked`, `x_unavailable` (X API out of credits / 402 / 429), `server_error`.
- `GET /healthz` → `{"ok":true}`
- `GET /v1/config` → `{"x":{"handle":"gurusharan","intent_url":"https://x.com/intent/post?text=...","preset_text":"..."},"linkedin":{"share_url":"https://www.linkedin.com/sharing/share-offsite/?url=...","preset_text":"...","prefill_text":false},"match":{"hashtag":"#AgentComputerUse","ref_links":["...?ref=acu-x","...?ref=acu-li"]},"providers":["x","linkedin"],"grace_days":14}`
- `GET /v1/auth/{x|linkedin}/start?device_id=<hex64>&device_name=<str>&app_challenge=<b64url S256>&link=<0|1>` → 302 to provider. `link=1` requires header-less session: pass `session=<token>` query (short-lived) to link the identity to that account.
- `GET /v1/auth/{p}/callback?code&state` → 302 `acu-island://auth?code=<one-time>` or `acu-island://auth?error=<code>`.
- `POST /v1/session/redeem {code, app_verifier, device_id, device_pubkey}` (device_pubkey = base64 raw P-256 public key, x963) → `{"session":"<opaque>","account":{"id","provider","handle","name","linked":["x"]},"status":"needs_verify|pending_review|unlocked","entitlement":"<token>|null"}`
- `POST /v1/verify/x` (Authorization: Bearer <session>) → 200 `{"status":"unlocked","entitlement":"..."}` or 422 `{"error":"follow_missing"|"post_missing","follow":bool,"post":bool}`
- `POST /v1/verify/linkedin {post_url}` (Bearer) → 200 unlocked, 202 `{"status":"pending_review"}`, 422 `post_url_invalid|post_url_used|rejected`
- `GET /v1/status` (Bearer) → `{"status": ..., "entitlement": ...}` (app polls pending every 60 s while the sheet is open)
- Unlock from verify/status may return 409 `device_conflict` or `device_taken`.
- `POST /v1/device/rebind` (Bearer) `{device_id, ts, sig}` → 200 `{"status":"unlocked","entitlement"}` or 429 `rebind_cooldown`
- `POST /v1/entitlement/refresh {device_id, ts, sig}` (no session; sig = base64 DER ECDSA P-256 SHA-256 over `"acu-gate-refresh|<device_id>|<ts>"`, ts unix seconds within ±300 s) → 200 `{"entitlement"}` | 409 `device_moved` | 403 `revoked` | 404 `not_found`. Rebind sig message: `"acu-gate-rebind|<device_id>|<ts>"`.
- `GET /v1/public/counter` → `{"unlocks":n,"updated_at":"ISO"}` CORS `*`, max-age 60.
- Admin (Bearer `ADMIN_TOKEN`): `GET /v1/admin/pending`, `POST /v1/admin/pending/{id}/approve|reject`, `POST /v1/admin/accounts/{id}/revoke`, `POST /v1/admin/accounts/{id}/reset-rebind`, `GET /v1/admin/x-owner/start` (Guru authorizes his own X account once for owned-read follower sync).
- Entitlement token: `base64url(payload_json) + "." + base64url(ed25519_sig over the first part)`; payload `{"v":1,"acct":"...","prov":"x|linkedin","handle":"...","device_id":"<hex64>","iat":unix,"exp":unix}`; app verifies with the compiled-in Ed25519 public key (raw 32 bytes, base64), DEBUG builds may override base URL + public key via `defaults write com.ingpoc.cua-service ACUGateDevBaseURL/ACUGateDevPublicKey` only when env `ACU_GATE_DEV=1`.

## Traction design (Guru, 2026-10-31)
- X is the primary route (big "Sign in with X" button). LinkedIn is a secondary "No X account?" text link.
- X follow of @gurusharan is required. Verified is NOT required.
- Each X unlock stores `verified`, `verified_type`, `public_metrics.followers_count` from `GET /2/users/me?user.fields=verified,verified_type,public_metrics` (same $0.010 call as sign-in, refreshed on each verify), for "verified unlocks" and "reach".
- LinkedIn route shows "Follow Gurusharan on LinkedIn" (opens config `linkedin.follow_url`, placeholder until Guru gives his profile URL) before "Share on LinkedIn". Not verified, never blocks.
- Ref click-throughs: `POST|GET /v1/public/ref-click?ref=acu-x|acu-li` → 204, CORS `*`, counts per ref per UTC day, no IP or cookie stored; only known refs counted. The site calls it with `navigator.sendBeacon` when `?ref=` is present (snippet `services/acu-gate/site/ref-beacon-snippet.html`; site deploy waits for Guru).
- `GET /v1/admin/stats` (Bearer `ADMIN_TOKEN`) → `{"unlocks":{"x":n,"linkedin":n,"total":n},"verified_unlocks":n,"verified_by_type":{"blue":n,...},"reach":sum_followers_of_x_unlocks,"pending":n,"ref_clicks":{"acu-x":n,"acu-li":n},"ref_clicks_by_day":[...]}`

## Site stats: downloads, unlocks, wall

`GET /v1/public/site-stats` returns downloads (`github_dmg`, `redirect_hits`, and their sum `total`), total unlocks, an opt-in user wall (newest first, at most 200), and `updated_at`. It has CORS `*` and a 60-second public cache lifetime. GitHub release asset totals include only `AgentComputerUse-*.dmg` assets under `agent-computer-use-v` tags, follow pagination, and cache for 15 minutes while retaining the last successful value on error. `GET /v1/public/download` counts a daily redirect hit and redirects to the latest stable ACU release's DMG. These are download events, not unique installations.

Wall consent defaults to false. Verify requests may set `show_on_wall`; `POST /v1/wall {show}` changes consent immediately on the server using a Bearer session or the bound device key. Device authorization includes `device_id`, Unix `ts` within 300 seconds, and base64 DER P-256 SHA-256 `sig` over `acu-gate-wall|<device_id>|<ts>|<true|false>`; replay protection also applies. The wall snapshots handle/name/avatar/profile at unlock, allows only HTTPS avatars on `pbs.twimg.com` or `media.licdn.com`, and excludes opted-out or revoked accounts. LinkedIn profile URLs remain null when unavailable. The embeddable site stats snippet uses DOM text and attribute setters; profile links have `rel="noopener nofollow ugc"`. Admin stats additionally exposes downloads and `wall_opt_in`. Site deployment still requires Guru's approval.
