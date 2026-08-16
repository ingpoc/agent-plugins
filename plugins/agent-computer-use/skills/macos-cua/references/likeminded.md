# Like-minded-app — CUA (repo scripts)

**Owner:** `Like-minded-app/script/macos_cua_*` + `build-macos-app` skill. Skill owns generic `macos-cua.py`; repo owns Likeminded harness.

## Lanes

| Goal | Command |
| --- | --- |
| One ledger **screen** capture/stamp | `macos_cua_preflight.sh` → `macos_audit_prepare.sh <screen>` → `macos_cua_screen.sh <screen>` |
| One ledger **flow** (interaction) | Repo `@testing-ledger` → `npm run testing:ledger-run -- --platform macos` (explicit prove branch required) |
| Full E2E flow | `bash script/macos_cua_manual_test.sh` (or `run_onboarding_flow_test.sh`) |
| API | `npm run dev:api:validation` on `:8787` |

**Do not** use `macos_cua_screen.sh` as a fallback for an unmatched `<screen>/<flow>` in `testing_ledger_prove_flow.sh` — that path false-passes by stamping unrelated controls.

## Session start (every run)

```bash
npm run dev:api:validation
./script/macos_cua_preflight.sh          # daemon + env defaults (follow-window, pixel click)
source script/macos_cua_helpers.sh       # after macos_canonical_app.sh
macos_cua_session_start                  # repo focus + canonical window/PID gate + display + reset
```

**Pointer smoke (validate dual-monitor + single pointer):**

```bash
npm run macos:cua-pointer-smoke
```

Pass: 3 tab clicks, `method=agent-cursor-glide+ax-click`, each followed by screen-text verification (`MEET`, `CIRCLES`, `COMMUNITIES`), and `MACOS_CUA_DISPLAY` matches app monitor (not hardcoded DELL).

Helpers: `script/macos_cua_helpers.sh` (source after `macos_canonical_app.sh`):

| Helper | Purpose |
| --- | --- |
| `macos_cua_export_display_env` | Parent-shell `MACOS_CUA_DISPLAY` after focus subprocess |
| `macos_wait_main_window` | Poll window (replaces `sleep 5`) |
| `macos_cua_launch_dev <token> <name>` | Kill → open with dev auth → place app window |
| `macos_cua_session_start` | repo focus + restore usable window if needed + export display + reset + canonical window/PID gate |
| `assert_basics_saved_api` | `GET /v1/me/profile` after Continue |
| `cua_click_last_label "<label>"` | Modal confirm when label duplicates sidebar |
| `cua_dismiss_modal` | Click Cancel (ledger settings proof) |

## E2E test

```bash
CUA_CAPTURE=failures bash script/macos_cua_manual_test.sh   # default: screenshots on fail only
CUA_CAPTURE=all OUT=output/my-run bash script/macos_cua_manual_test.sh
CUA_TAB_NAV=1 bash script/macos_cua_manual_test.sh        # optional tab-nav proof
```

Pass (2026-07-06): **27 steps, ~2.5 min**, `fail=0`. Flow: Profile → onboarding → voice → tabs → sign-out → re-auth → delete → welcome screen.

## Env

Set by `macos_cua_preflight.sh` (do not duplicate here). Key:
`MACOS_CUA_DISPLAY`, `MACOS_CUA_MAX_MODAL=80`. Legacy overlay variables may
remain in older repo scripts but do not control the signed operator cursor.

## App resolution

| Layer | Name |
| --- | --- |
| Bundle / executable | `LikemindedMac` |
| Window owner (Quartz) | `Likeminded` |
| CUA app arg | `com.likeminded.mac` (exact bundle identity) |

Repo session start focuses through the repo owner, resets CUA resolution, then validates the canonical `LikemindedMac` PID/window through `macos_cua_window.sh` without a redundant CUA focus call. Interaction commands still use the exact bundle id. The Quartz owner label remains window-resolution metadata only.

Foreground AX-dependent CUA commands dispatch NSWorkspace activation plus a three-second PID-specific System Events foreground request for an existing app without requiring immediate `isActive`, resolve its authoritative PID/window through Quartz without a pre-driver synchronous AX query, then let bounded driver `bring_to_front` plus fresh snapshot prove foreground state. New-app `open` and AppleScript activation are separately limited to five seconds. `get_window_state` defaults to a 12-second timeout and does not retry driver errors. If a driver inventory exposes only application/menu scaffolding, `list-buttons` and label clicks reactivate the exact PID and use bounded native AX inventory/press without another driver call between activation and AX access. If native AX confirms the running process has no window, the wrapper sends one bounded bundle reopen event and retries native inventory. When WindowServer still has a live rendered window but AX remains menu-only, the wrapper asks the already-permitted driver to write the window screenshot using its strict `capture_mode=vision` schema, verifies PID-owned Quartz bounds, then runs a cached native Swift Apple Vision helper on that file when the driver supplies no framed labels. Later fresh destination state still owns proof. Empty results from every route fail rather than masquerading as zero-control screens.

## AX notes

- **Profile before** `Start profile setup` (not from Meet tab).
- RSVP: `Saturday Community meetup available` / `not available`.
- Settings modals: `--max 80`; Sign out confirm index ~36.
- Duplicate `Delete account`: `cua_click_last_label` after opening modal.
- Ledger settings: Log out / Delete → **Cancel** (proof without killing session).
- `type-label` for fields; `click-label-pointer` for navigation.
- Sequential only: `npm run macos:validation-batch`.

## Assertions (do not trust AX alone)

| Check | How |
| --- | --- |
| Basics saved | `assert_basics_saved_api` — `PATCH /v1/me/profile` creates draft profile on first save |
| Placement | `GET /v1/me/placement` with flow token |
| After delete | `ui_tree_contains "Sign in with Apple"` |
| Stale banner | UI may show `"Profile could not be saved"` while API save succeeded — prefer API assert |

## Deep links

`--mac-screen <name>` **before** other launch flags. See `build-macos-app` skill + ledger `source_files`.
