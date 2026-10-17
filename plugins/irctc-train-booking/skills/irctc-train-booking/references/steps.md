# Manual steps + selectors (repair only)

No fixed sleeps: wait for the next element (`wait_for_selector`) or poll a read-only evaluate; add ~300-700 ms settle only before clicking an animating dialog/dropdown. On `ACTIONABILITY_UNSTABLE` retry that click once. GENERAL quota is the default: don't touch the quota dropdown.

What `book.py` does, in order. Use with `~/irctc/send.sh file.json` (needs `WORK`) to repair one broken step, then fix `book.py` and this file.

## Gotchas
- Password-manager/iCloud Passwords extension must be OFF in Comet (its iframe blocks CDP: `foreign-extension URL/frame restriction`).
- Any reload/`goto` after login logs you out. Order: log in, then fill search (no reload in between).
- `evaluate` must be read-only: `.value` on inputs and some `.map` calls throw `Possible side-effect`; use `getAttribute` or `page_context`.
- Clipboard paste never lands in IRCTC fields; `login.py` types via `cursor_type`.
- After login IRCTC may show a "Last Transaction Detail" (or other) popup with a `Close` button; it covers the form (`ACTIONABILITY_OBSCURED`). `book.py` only handles popups on failure: screenshot, click visible Close/OK, Escape, retry the step (up to 3x). Never pre-check every step.
- Narrow window (950px): LOGIN sits behind the hamburger at (width-33, 95); scroll to top first (`cursor_scroll deltaY -4000`) or the click lands on the banner. Logged-in check = `Welcome <Name>` or `logout` in `document.body.textContent` (innerText misses the hidden menu).
- Never close the window on failure; leave it for the passenger. One window: `~/irctc/.lease` holds the WORK dir, reused if alive and on `/train-search`.

## 1. Start + login (no captcha/OTP seen)
1. Probe, one durable `start` on `https://www.irctc.co.in/nget/train-search`, wait for `.ui-dialog button`, settle 700.
2. `click_text English` (language alert appears on every load), then `click_text "LOGIN / REGISTER"`, wait for `input[formcontrolname='userid']`.
3. `python3 ~/irctc/login.py`.
4. `click_selector app-login button.search_btn`, poll until success: `app-header` text has `Welcome <name>`. If `Invalid Request`: reload, redo 2-4 once.

## 2. Search form (same page, no reload)
5. Stations: click `input[aria-label='Enter From station. Input is Mandatory.']`, cmd+A, `cursor_type` code (CSMT/PUNE/ADI), wait for `.ui-autocomplete-panel li`, `cursor_key Enter` (first suggestion). Same for `input[aria-label='Enter To station. Input is Mandatory.']`.
6. Date: click `p-calendar input`, cmd+A, type `DD/MM/YYYY`, Escape.
7. Quota (Tatkal only): click `#journeyQuota`, wait for its `li[aria-label]`, settle 350, click `#journeyQuota li[aria-label='TATKAL']` (default GENERAL). Class: `#journeyClass` then `#journeyClass li[aria-label='AC 3 Tier (3A)']`.
8. Confirm: evaluate `#journeyClass` / `#journeyQuota` innerText.

## 3. Train list
9. `click_text "Search Trains"` -> `/nget/booking/train-list`.
10. evaluate: `Array.from(document.querySelectorAll('app-train-avl-enq')).map(e=>{var g=e.parentElement.parentElement;return (Array.prototype.indexOf.call(g.parentElement.children,g)+1)+' '+e.innerText.split('\n')[0]+' | '+Array.from(e.querySelectorAll('td strong')).map(s=>s.innerText).join(',')})` -> card nth-child N and class order.
11. Class cell: `div.ng-star-inserted:nth-child(N) > div > app-train-avl-enq td:nth-child(C) div.pre-avl` (C = class position), poll card text for AVAILABLE/WL/RAC/REGRET. Tatkal before opening: `Date outside Tatkal ARP- (50018)`.
12. Date cell (enables Book Now): `div.ng-star-inserted:nth-child(N) > div > app-train-avl-enq td.link:nth-child(2) div.pre-avl`.
13. `div.ng-star-inserted:nth-child(N) > div > app-train-avl-enq button.btnDefault.train_Search` (Book Now; WL trains also show a `CNF Probability` btnDefault) -> `/nget/booking/psgninput`.

## 4. Passenger -> payment
14. Click `input[placeholder='Full Name as per Govt. ID']`, cmd+A, type the saved-passenger prefix, wait for `.ui-autocomplete-panel li.ui-autocomplete-list-item`, click `.ui-autocomplete-panel li.ui-autocomplete-list-item:first-child` (fills name/age/gender from saved list). Enter key does not pick it.
15. UPI: click `p-radiobutton[formcontrolname=paymentType][id='2'] .ui-radiobutton-box` (id 3 = cards, default). Label clicks do not toggle. Insurance default Yes (₹0.45); No = `#travelInsuranceOptedNo-0 .ui-radiobutton-box`.
16. `click_text Continue`, `wait_for_url_change` (timeout 40000) -> `/nget/booking/reviewBooking` (check total; UPI fee ₹23.60 vs card ₹35.40).
17. Aadhaar OTP block (Tatkal, verified 2026-10-11; no captcha): click `input[formcontrolname=aadhaarConsent]`, `click_text "Generate OTP"` (toast: OTP sent to the Aadhaar-linked mobile). Continue stays disabled until the OTP is in.
18. OTP arrives as SMS from `ADHAAR-S` ("NNNNNN is OTP for Aadhaar ... valid 10 mins"). It does NOT sync to the Mini's Messages DB; read it via iPhone Mirroring (window at 2052,326 size 322x712; `screencapture -x -R2052,326,322,712`, Messages app is open on Transactions). `otp_watch.py` does this automatically (baseline OTP first, then `ocr` Vision OCR every ~0.5 s on region 2052,380,322,180, writes the first new OTP to `$IRCTC_OTP_FILE`); `book.py` types one digit per box `#otp-0`..`#otp-5` (cmd+A, type digit), then `Continue`. Re-send: `click_text "Resend OTP"` (60 s cooldown). A stale review page (~30 min) returns `Unable to process your request`: start a fresh run.
19. `/nget/payment/bkgPaymentOptions`: click `div.border-all.col-xs-12.link.no-pad` (IRCTC iPay New), `click_text "Pay & Book"` -> `pg.irctcpayments.com` (UPI + Pay Now). STOP; the passenger pays.

