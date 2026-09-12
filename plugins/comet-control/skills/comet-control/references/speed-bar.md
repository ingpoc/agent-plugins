# Comet Control — Speed bar (agent loop)

Load when campaigns feel slow. Healthy `page_context` / locator ops are usually
sub-second. Field logs (2026-09-12, Efficient + ACU) show perceived slowness is
**agent poll/remint noise, screenshot-every-step, failed waits (16–87s)** — not
routine plugin latency.

## Measured (controller.log, 1s resolution)

| Lease | n | Pattern |
| --- | --- | --- |
| `aadhar-portal-huf-20260912` | 190 | ~92% wall in think-gaps; **70×** `page_context+evaluate` + **46×** `native_handoff` while ACU held GSI picker (poll loop) |
| `aadhar-render-pem-copy-20260912` | 18 | failed `click_text+wait+page_context` **16s**; else ≤1s |
| `careerops-apply-20260910` | 50 | locator/page_context healthy ≤1–2s; batches with wait+screenshot **5–6s** |
| `smm-x-comet-article-20260910-0915` | — | cursor/page_context ≤1s when lease healthy |

Slow classes: `activate_tab` into other-session Google tab **87s** err; lone
`screenshot` max **34s** (historical opening `page_context+screenshot` hung ~3m
on `captureVisibleTab` — now bounded 8s, still a class risk).

## Bans (do not)

1. **Other-tab focus** — never `focus_tab` / `activate_tab` into a Google Accounts
   / GSI popup tab. Lease is tab-scoped. Other-window picker →
   [`google-accountchooser-ax.md`](google-accountchooser-ax.md).
   Footgun fixed: SW normalizes `tab_id`→`tabId` and rejects foreign activate/focus with
   `LEASE_TAB_SCOPED` (was ~87s `EXTENSION_TIMEOUT` on `activate_tab`+snake `tab_id`).
2. **Screenshot every step** — do not bolt `screenshot` onto every click/wait
   batch (inflates ~1s → 5–6s). Screenshots only for visual claims; prefer CDP
   after `viewport_set` (see [`operate.md`](operate.md)). Skip opening screenshots.
3. **Poll OS/CUA at 12s** — while waiting on human/OS/CUA, ≤**1 observe / 30s**
   (one compact `page_context`). Do not `page_context+evaluate` (+ `native_handoff`)
   in a tight loop.
4. **Fixed 2–5s sleeps** for readiness — use `wait_for_selector` /
   `wait_for_url_change`. Cosmetic `wait` ≤400ms.
5. **Dual Browser-Use + durable `send`** as concurrent owners — causes IPC
   timeouts. One path only.
6. **Remint on `EXTENSION_TIMEOUT`** — wait/restore same session; see
   [`optimize.md`](optimize.md).
7. **Wrong cmd shape** — `{"actions":[...]}` only (bare `"action"` fails closed).

## Do this

1. **Act within 2 observes.** After two observes with no click/type/goto, change
   strategy or hand off — do not open a tenth observe.
2. **One orientation read:** single `page_context`. Do not pair
   `page_context` + full-body `evaluate` + `screenshot` every step.
3. **Batch** one `actions[]`: click/locator + compact `page_context`; batch
   `dialog_handle` with the click that opens it.
4. **Timeouts:** 30–60s for simple reads/clicks; 90–180 only for known SPA
   remounts. Default `--timeout 180` is hang masking, not a target.
5. **One durable controller** for the campaign. Idle ready→closeout with zero
   cmds wastes the runtime — act or closeout.
6. **API-first** when the fact is JSON — do not lease.

## Plugin suspects (verified paths; code cut optional)

| Suspect | Path | Note |
| --- | --- | --- |
| Other-tab activate/focus | `service_worker.js` `LEASE_TAB_SCOPED` | Shipped — fail-fast |
| Default send timeout 180 | `durable_lease_controller.py` `--timeout` default | Optionally shorter when actions are observe-only |
| `captureVisibleTab` minGap 550ms + 8s bound | `service_worker.js` | Prefer CDP screenshot for leased proof after `viewport_set` |
| `moveToAndWait` 900ms | SW + `cursor-agent.js` | Secondary vs agent gaps; consider lower for non-demo clicks |
| Locator fill ~8s bound | operate.md | Fail-fast; do not use fill as happy path for long contenteditable |

Cursor 100–400ms settles and broker `InvalidUpgrade` probe noise are **not** the
main feel. Do not lengthen SKILL.md — keep this reference + product needles.
